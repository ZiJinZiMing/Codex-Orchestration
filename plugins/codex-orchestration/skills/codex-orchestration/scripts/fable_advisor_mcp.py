#!/usr/bin/env python3
"""Read-only MCP bridge from Codex to Claude Fable 5."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from configure_fable_api import (  # noqa: E402
    FableApiConfigError,
    load_config as load_fable_api_config,
)
import routing_state  # noqa: E402


STATE_FILENAME = ".codex-orchestration-routing.json"
MANAGED_MARKER = routing_state.MANAGED_MARKER
FABLE_MODEL = "claude-fable-5"
ALLOWED_MODELS = frozenset({FABLE_MODEL})
ALLOWED_DIRECT_RESPONSE_MODELS = frozenset(
    {FABLE_MODEL, f"anthropic/{FABLE_MODEL}"}
)
ADVISOR_PATHS = {"claude-code-cli", "ccswitch", "python-api"}
ADVISOR_PATH_DISPLAY = {
    "claude-code-cli": "Claude Code CLI",
    "ccswitch": "CCSwitch",
    "python-api": "Python API",
}
REVIEW_SESSION_NAME = "codex-fable-review"
SUPPORTED_EFFORTS = frozenset(routing_state.FABLE_EFFORTS) | {"low", "medium", "high", "max"}
FABLE_SERVERS = routing_state.FABLE_SERVERS
FABLE_HELPER_MODEL = "claude-haiku-4-5-20251001"
ALLOWED_RUNTIME_MODELS = frozenset({FABLE_MODEL, FABLE_HELPER_MODEL})
AUTH_MODES = {"subscription", "api", "auto"}
API_SOURCES = {"config-file", "environment", "user-settings"}
TRANSPORTS = {"claude-code", "direct-api"}
CLAUDE_TIMEOUT_SECONDS = 600
AUTH_TIMEOUT_SECONDS = 20
DIRECT_API_TIMEOUT_SECONDS = 600
DIRECT_API_MAX_TOKENS = 131072
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
API_CREDENTIAL_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
}
API_TRANSPORT_ENV = API_CREDENTIAL_ENV | {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
}
OAUTH_OVERRIDE_ENV = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
}
PROVIDER_OVERRIDE_ENV = {
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
}
CLAUDE_MODEL_ENV = {
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
}
SENSITIVE_ENV = API_CREDENTIAL_ENV | OAUTH_OVERRIDE_ENV | PROVIDER_OVERRIDE_ENV
MAX_INPUT_CHARS = 200_000
ADVISOR_SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a plan advisor to Codex's root orchestrator.
Review the supplied self-contained packet for material correctness, missing constraints, unsafe sequencing, ownership conflicts, and verification gaps. Do not edit files, call tools, spawn agents, contact executors, or attempt implementation.

Your first non-empty line must be exactly PLAN_APPROVED or PLAN_REVISE.
Use PLAN_APPROVED only when no material gap is present. Use PLAN_REVISE when correction is needed. For PLAN_REVISE, assign every material finding a stable, unique finding ID and give a concrete correction. On later rounds, preserve IDs from the supplied cumulative ledger. Ignore style preferences. Report only to the root orchestrator."""

PLANNER_CREATE_SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a plan author for Codex's root orchestrator.
Create a concrete implementation plan from the supplied self-contained packet. Include constraints, ownership, sequencing, acceptance criteria, security and compatibility boundaries, and behavioral plus regression verification. Do not edit files, call tools, spawn agents, contact the Advisor or executors, or attempt implementation.

Your first non-empty line must be exactly PLAN_DRAFT. Return the complete draft plan after that signal. Report only to the root orchestrator."""

PLANNER_REVISE_SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a stateless plan reviser for Codex's root orchestrator.
Revise the supplied canonical current plan using the original task, its source plan version, the latest Advisor critique, and the compact cumulative history. Do not edit files, call tools, spawn agents, contact the Advisor or executors, or attempt implementation.

Your response must use exactly this top-level structure:
PLAN_REVISION

## FINDINGS_LEDGER
For every finding in the latest critique, include its stable Advisor finding ID exactly once and mark it INCORPORATED or REJECTED. Give a concrete reason for either disposition. Preserve relevant cumulative-history IDs.

## REVISED_PLAN
Provide the complete revised plan, clearly identifying its source plan version and revised version.

Both sections must be non-empty. Your first non-empty line must be exactly PLAN_REVISION. The root orchestrator, not you, validates finding coverage and plan-version semantics. Report only to the root orchestrator."""

# Backward-compatible public constant for existing importers.
SYSTEM_PROMPT = ADVISOR_SYSTEM_PROMPT

Seat = Literal["planner", "advisor"]


class AdvisorError(RuntimeError):
    pass


def _safe_refusal_field(value: Any, max_chars: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:max_chars]


class NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Fail closed instead of forwarding API credentials across redirects."""

    def redirect_request(
        self,
        req: urllib_request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def advisor_path_for(transport: str, api_source: str | None) -> str:
    if transport == "claude-code":
        return "claude-code-cli"
    if transport == "direct-api" and api_source == "user-settings":
        return "ccswitch"
    if transport == "direct-api" and api_source in {"config-file", "environment"}:
        return "python-api"
    raise AdvisorError("The saved Claude Fable 5 advisor path is invalid.")


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def environment_for_auth_path(
    auth_path: str, api_source: str | None = None
) -> dict[str, str]:
    if auth_path not in {"subscription", "api"}:
        raise AdvisorError(f"Unsupported Claude authentication path: {auth_path!r}.")
    env = os.environ.copy()
    for name in PROVIDER_OVERRIDE_ENV:
        env.pop(name, None)
    if auth_path == "subscription":
        excluded = API_TRANSPORT_ENV
    elif api_source == "user-settings":
        excluded = API_TRANSPORT_ENV | OAUTH_OVERRIDE_ENV
    else:
        excluded = OAUTH_OVERRIDE_ENV
    for name in excluded:
        env.pop(name, None)
    return env


def sanitized_environment() -> dict[str, str]:
    """Backward-compatible subscription environment used by older callers."""
    return environment_for_auth_path("subscription")


def strict_review_environment(
    auth_path: str, api_source: str | None = None
) -> dict[str, str]:
    """Pin every Claude Code model slot to Fable and disable auxiliary traffic."""
    env = environment_for_auth_path(auth_path, api_source)
    env.update(
        {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
        }
    )
    for name in CLAUDE_MODEL_ENV:
        env[name] = FABLE_MODEL
    return env


def user_settings_api_invocation(
    home: Path | None = None,
) -> tuple[dict[str, str], str | None]:
    """Extract only API transport values needed for an isolated Claude invocation."""
    settings = _read_settings(
        (home or Path.home()) / ".claude" / "settings.json",
        "Claude Code user settings",
    )
    settings_env = settings.get("env")
    api_env = {
        name: value
        for name in API_TRANSPORT_ENV
        if isinstance(settings_env, dict)
        and isinstance((value := settings_env.get(name)), str)
        and value
    }
    helper = settings.get("apiKeyHelper")
    return api_env, helper if isinstance(helper, str) and helper else None


def api_invocation_for_source(
    api_source: str, home: Path | None = None
) -> tuple[dict[str, str], str | None]:
    if api_source == "environment":
        return (
            {
                name: value
                for name in API_TRANSPORT_ENV
                if isinstance((value := os.environ.get(name)), str) and value
            },
            None,
        )
    if api_source == "user-settings":
        return user_settings_api_invocation(home)
    raise AdvisorError(f"Unsupported Claude API source: {api_source!r}.")


def _standalone_api_config(home: Path | None = None) -> dict[str, Any]:
    try:
        return load_fable_api_config(
            codex_home=codex_home() if home is None else home
        )
    except FableApiConfigError as exc:
        raise AdvisorError(str(exc)) from exc


def check_api_auth(
    api_source: str | None, home: Path | None = None
) -> dict[str, str]:
    if api_source not in API_SOURCES:
        raise AdvisorError(
            "Claude api mode requires an explicit API source: config-file, environment, "
            "or user-settings."
        )
    if api_source == "config-file":
        config = _standalone_api_config(home)
        if not config["enabled"]:
            raise AdvisorError(
                "Python API path is disabled because the provider api_key is empty."
            )
        return {
            "auth_mode": "api",
            "auth_path": "api",
            "auth_method": "api",
            "api_source": api_source,
        }
    api_sources = set(api_credential_sources(home))
    if api_source not in api_sources:
        raise AdvisorError(
            f"Claude API credentials are not configured in the saved {api_source} source."
        )
    conflicting_sources = set(api_route_sources(home)) - {api_source}
    if conflicting_sources:
        raise AdvisorError(
            "Claude API/Gateway configuration exists in more than the saved source; "
            "remove the conflicting source before using api mode."
        )
    return {
        "auth_mode": "api",
        "auth_path": "api",
        "auth_method": "api",
        "api_source": api_source,
    }


def _direct_api_endpoint(base_url: str) -> str:
    try:
        parsed = urllib_parse.urlsplit(base_url)
    except ValueError as exc:
        raise AdvisorError("Direct API base URL is invalid.") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AdvisorError("Direct API base URL is invalid.")
    if parsed.scheme == "http":
        if parsed.hostname not in LOCAL_HTTP_HOSTS:
            raise AdvisorError("Direct API requires HTTPS except for exact localhost hosts.")
    elif parsed.scheme != "https":
        raise AdvisorError("Direct API base URL must use HTTPS.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AdvisorError("Direct API base URL is invalid.") from exc
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urllib_parse.urlunsplit(
        (parsed.scheme, authority, f"{path}/v1/messages", "", "")
    )


def direct_api_configuration(
    api_source: str, home: Path | None = None
) -> tuple[dict[str, str], str, dict[str, str]]:
    if api_source == "config-file":
        config = _standalone_api_config(home)
        if not config["enabled"]:
            raise AdvisorError(
                "Python API path is disabled because the provider api_key is empty."
            )
        provider = config["provider"]
        auth = {
            "auth_mode": "api",
            "auth_path": "api",
            "auth_method": "api",
            "api_source": api_source,
            "advisor_path": "python-api",
            "request_model": provider["model"],
        }
        if provider["auth_type"] == "bearer":
            credential_header = {
                "Authorization": f"Bearer {provider['api_key']}"
            }
        else:
            credential_header = {"x-api-key": provider["api_key"]}
        return credential_header, provider["api_url"], auth

    auth = check_api_auth(api_source, home)
    api_env, api_key_helper = api_invocation_for_source(api_source, home)
    if api_key_helper:
        raise AdvisorError("Direct API does not support Claude Code apiKeyHelper.")
    if api_env.get("ANTHROPIC_CUSTOM_HEADERS"):
        raise AdvisorError("Direct API does not support ANTHROPIC_CUSTOM_HEADERS.")
    auth_token = api_env.get("ANTHROPIC_AUTH_TOKEN")
    api_key = api_env.get("ANTHROPIC_API_KEY")
    if auth_token and api_key:
        raise AdvisorError("Direct API credential source is ambiguous.")
    if auth_token:
        credential_header = {"Authorization": f"Bearer {auth_token}"}
    elif api_key:
        credential_header = {"x-api-key": api_key}
    else:
        raise AdvisorError("Direct API requires one static API credential.")
    endpoint = _direct_api_endpoint(
        api_env.get("ANTHROPIC_BASE_URL", DEFAULT_ANTHROPIC_BASE_URL)
    )
    auth["request_model"] = FABLE_MODEL
    auth["advisor_path"] = advisor_path_for("direct-api", api_source)
    return credential_header, endpoint, auth


def api_credential_sources(home: Path | None = None) -> tuple[str, ...]:
    """Return only non-secret source labels for configured Anthropic API credentials."""
    sources: set[str] = set()
    if any(os.environ.get(name) for name in API_CREDENTIAL_ENV):
        sources.add("environment")

    settings_path = (home or Path.home()) / ".claude" / "settings.json"
    payload = _read_settings(settings_path, "Claude Code user settings")
    settings_env = payload.get("env")
    settings_has_credential = isinstance(settings_env, dict) and any(
        settings_env.get(name) for name in API_CREDENTIAL_ENV
    )
    if settings_has_credential or payload.get("apiKeyHelper"):
        sources.add("user-settings")
    for path in _managed_settings_paths():
        managed = _read_settings(path, "Claude Code managed settings")
        managed_env = managed.get("env")
        if (
            isinstance(managed_env, dict)
            and any(managed_env.get(name) for name in API_CREDENTIAL_ENV)
        ) or managed.get("apiKeyHelper"):
            sources.add("managed-settings")
    return tuple(sorted(sources))


def _read_settings(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdvisorError(f"Could not inspect {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdvisorError(f"{label} must contain a JSON object.")
    return payload


def _managed_settings_paths() -> tuple[Path, ...]:
    paths = {
        Path("/etc/claude-code/managed-settings.json"),
        Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    }
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        paths.add(Path(program_data) / "ClaudeCode" / "managed-settings.json")
    return tuple(sorted(paths, key=str))


def api_route_sources(home: Path | None = None) -> tuple[str, ...]:
    """Return non-secret sources that can redirect or authenticate Claude API traffic."""
    sources: set[str] = set()
    if any(os.environ.get(name) for name in API_TRANSPORT_ENV):
        sources.add("environment")
    settings = _read_settings(
        (home or Path.home()) / ".claude" / "settings.json",
        "Claude Code user settings",
    )
    settings_env = settings.get("env")
    if (
        isinstance(settings_env, dict)
        and any(settings_env.get(name) for name in API_TRANSPORT_ENV)
    ) or settings.get("apiKeyHelper"):
        sources.add("user-settings")
    for path in _managed_settings_paths():
        managed = _read_settings(path, "Claude Code managed settings")
        managed_env = managed.get("env")
        if (
            isinstance(managed_env, dict)
            and any(managed_env.get(name) for name in API_TRANSPORT_ENV)
        ) or managed.get("apiKeyHelper"):
            sources.add("managed-settings")
    return tuple(sorted(sources))


def resolve_claude() -> Path:
    found = shutil.which("claude")
    if found:
        return Path(found).resolve()
    candidates = (
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AdvisorError("Claude Code is not installed or `claude` is not on PATH.")


def _run_json(
    command: list[str], *, timeout: int, env: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            env=env or sanitized_environment(),
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdvisorError("Claude Code authentication check timed out; output withheld.") from exc
    except OSError as exc:
        raise AdvisorError("Could not run Claude Code authentication check; output withheld.") from exc
    if result.returncode != 0:
        raise AdvisorError(f"Claude Code exited with {result.returncode}.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError("Claude Code returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise AdvisorError("Claude Code returned an unexpected JSON value.")
    return payload


def _local_claude_subscription() -> str | None:
    """Read only the subscription class from Claude Code's account metadata."""
    try:
        payload = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    account = payload.get("oauthAccount")
    if not isinstance(account, dict):
        return None
    organization_type = account.get("organizationType")
    if organization_type == "claude_pro":
        return "pro"
    if organization_type == "claude_max":
        return "max"
    return None


def check_claude_auth(
    claude: Path | None = None,
    auth_mode: str = "subscription",
    api_source: str | None = None,
) -> dict[str, str]:
    if auth_mode not in AUTH_MODES:
        raise AdvisorError(f"Unsupported Claude authentication mode: {auth_mode!r}.")
    if api_source is not None and api_source not in API_SOURCES:
        raise AdvisorError(f"Unsupported Claude API source: {api_source!r}.")
    if auth_mode != "api" and api_source is not None:
        raise AdvisorError("Claude API source is valid only in api authentication mode.")
    route_sources = set(api_route_sources())
    if auth_mode == "auto" and route_sources:
        raise AdvisorError(
            "Claude API/Gateway configuration is present; choose api mode and an API source "
            "explicitly instead of allowing auto to select a potentially metered path."
        )
    auth_path = "subscription" if auth_mode == "auto" else auth_mode
    if auth_path == "api":
        result = check_api_auth(api_source)
        result["auth_mode"] = auth_mode
        return result
    if route_sources:
        raise AdvisorError(
            "Claude API/Gateway configuration is present, so subscription mode cannot prove that the "
            "model call will use subscription billing; choose api mode and an API source "
            "explicitly, or remove the API configuration."
        )

    executable = claude or resolve_claude()
    payload = _run_json(
        [str(executable), "auth", "status"],
        timeout=AUTH_TIMEOUT_SECONDS,
        env=environment_for_auth_path("subscription"),
    )
    subscription = payload.get("subscriptionType")
    auth_method = payload.get("authMethod")
    legacy_subscription_login = auth_method == "claude.ai" and subscription in {"pro", "max"}
    current_subscription_login = (
        auth_method == "oauth_token"
        and _local_claude_subscription() in {"pro", "max"}
    )
    if not (
        payload.get("loggedIn") is True
        and payload.get("apiProvider") == "firstParty"
        and (legacy_subscription_login or current_subscription_login)
    ):
        raise AdvisorError(
            "Claude Code must be logged in through a first-party Pro or Max account; "
            "run `claude auth login` and try again."
        )
    return {
        "auth_mode": auth_mode,
        "auth_path": "subscription",
        "auth_method": "claude.ai",
        "api_provider": "firstParty",
    }


def _read_routing_state(home: Path | None = None) -> dict[str, Any]:
    root = home or codex_home()
    path = root / STATE_FILENAME
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AdvisorError("The saved routing state is not a regular file.")
        if info.st_nlink != 1:
            raise AdvisorError("The saved routing state has multiple hard links.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdvisorError("Claude Fable 5 is not configured; run setup first.") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdvisorError("Could not read valid routing state.") from exc
    if not isinstance(payload, dict):
        raise AdvisorError("The saved routing state is invalid.")
    try:
        state = routing_state.validate_routing_state(payload)
    except routing_state.RoutingStateError as exc:
        # Historical feature snapshots (schema 3/policy 2) predate the
        # combined schema 4 validator and carried only Advisor transport
        # metadata. Permit exactly those known route extensions as a narrow
        # compatibility bridge; schema 4 must always pass the shared validator
        # with its complete shape and must never reach this fallback.
        advisor = payload.get("advisor")
        if (payload.get("schema"), payload.get("policy_version")) != (3, 2) or not isinstance(advisor, dict):
            raise AdvisorError("The saved routing state is invalid.") from exc
        policy_payload = json.loads(json.dumps(payload))
        sanitized_advisor = policy_payload.get("advisor")
        if not isinstance(sanitized_advisor, dict) or sanitized_advisor.get("kind") != "fable":
            raise AdvisorError("The saved routing state is invalid.") from exc
        for key in ("auth_mode", "api_source", "transport", "path"):
            sanitized_advisor.pop(key, None)
        try:
            state = routing_state.validate_routing_state(policy_payload)
        except routing_state.RoutingStateError as compatibility_exc:
            raise AdvisorError("The saved routing state is invalid.") from compatibility_exc
    config_file = state["config_file"]
    try:
        belongs_to_home = (
            Path(config_file).expanduser().resolve()
            == (root / "config.toml").expanduser().resolve()
        )
    except (OSError, RuntimeError) as exc:
        raise AdvisorError("The saved routing state belongs to another Codex home.") from exc
    if not belongs_to_home:
        raise AdvisorError("The saved routing state belongs to another Codex home.")
    return payload


def _validate_seat(seat: str) -> Seat:
    if seat not in {"planner", "advisor"}:
        raise AdvisorError("Fable seat must be `planner` or `advisor`.")
    return seat  # type: ignore[return-value]


def _validate_fable_route(route: Any, *, seat: Seat) -> dict[str, str]:
    if not isinstance(route, dict) or route.get("kind") != "fable":
        raise AdvisorError(f"Claude Fable 5 is not the configured {seat}.")
    model = route.get("model")
    effort = route.get("effort")
    if model != FABLE_MODEL or effort not in SUPPORTED_EFFORTS:
        raise AdvisorError(f"The saved Claude Fable 5 {seat} route is invalid.")
    if seat == "planner":
        if set(route) != {"kind", "model", "effort", "server"}:
            raise AdvisorError("Planner Fable route has unsupported fields.")
        return {"model": model, "effort": effort}
    auth_mode = route.get("auth_mode", "subscription")
    api_source = route.get("api_source")
    transport = route.get("transport", "claude-code")
    saved_path = route.get("path")
    if auth_mode not in AUTH_MODES:
        raise AdvisorError("The saved Claude Fable 5 auth mode is invalid.")
    if auth_mode == "api" and api_source not in API_SOURCES:
        raise AdvisorError("The saved Claude Fable 5 API source is invalid.")
    if auth_mode != "api" and api_source is not None:
        raise AdvisorError("The saved Claude Fable 5 API source is unexpected.")
    if transport not in TRANSPORTS:
        raise AdvisorError("The saved Claude Fable 5 transport is invalid.")
    if transport == "direct-api" and auth_mode != "api":
        raise AdvisorError("Direct API transport requires Claude api authentication mode.")
    if api_source == "config-file" and transport != "direct-api":
        raise AdvisorError("The config-file API source requires direct-api transport.")
    if model != FABLE_MODEL or effort not in SUPPORTED_EFFORTS:
        raise AdvisorError("The saved Claude Fable 5 route is invalid.")
    advisor_path = advisor_path_for(transport, api_source)
    if saved_path is not None and saved_path != advisor_path:
        raise AdvisorError("The saved Claude Fable 5 advisor path is inconsistent.")
    result = {
        "model": model,
        "effort": effort,
        "auth_mode": auth_mode,
        "transport": transport,
        "path": advisor_path,
    }
    if api_source is not None:
        result["api_source"] = api_source
    return result


def load_fable_route(
    home: Path | None = None, *, seat: str = "advisor"
) -> dict[str, str]:
    selected = _validate_seat(seat)
    payload = _read_routing_state(home)
    return _validate_fable_route(payload.get(selected), seat=selected)


def _normalize_review(review: str) -> tuple[str, str]:
    review = review.strip()
    if not review:
        raise AdvisorError("Claude Fable 5 returned an empty review.")
    first = next((line.strip() for line in review.splitlines() if line.strip()), "")
    if first not in {"PLAN_APPROVED", "PLAN_REVISE"}:
        first = "PLAN_REVISE"
        review = f"{first}\n\n{review}"
    return first, review


def _validate_inputs(operation: str, **values: Any) -> dict[str, str]:
    checked: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise AdvisorError(f"`{name}` must be a non-empty string for {operation}.")
        checked[name] = value
    if sum(len(value) for value in checked.values()) > MAX_INPUT_CHARS:
        raise AdvisorError(
            f"{operation} input exceeds the {MAX_INPUT_CHARS}-character combined limit."
        )
    return checked


def _first_non_empty_line(response: str) -> str:
    return next((line.strip() for line in response.splitlines() if line.strip()), "")


def _validate_runtime_models(usage: Any, *, auth_path: str) -> list[str]:
    if not isinstance(usage, dict):
        raise AdvisorError(
            "Strict model verification failed: Claude Code omitted modelUsage metadata."
        )
    used_models = sorted(set(usage))
    if not all(isinstance(model, str) for model in used_models):
        raise AdvisorError(
            "Runtime metadata reported a model outside the allowed Fable runtime policy."
        )
    if FABLE_MODEL not in used_models:
        raise AdvisorError(
            "Runtime metadata did not confirm the pinned Claude Fable 5 primary model."
        )
    allowed_models = (
        ALLOWED_RUNTIME_MODELS if auth_path == "subscription" else {FABLE_MODEL}
    )
    if not set(used_models).issubset(allowed_models):
        if auth_path == "api":
            raise AdvisorError(
                "Strict model verification failed: expected only claude-fable-5, got "
                f"{used_models!r}. Advisor unavailable; executor work must remain blocked."
            )
        raise AdvisorError(
            "Runtime metadata reported a model outside the allowed Fable runtime policy."
        )
    return used_models


def _invoke_fable(
    *,
    operation: str,
    seat: Seat,
    prompt: str,
    system_prompt: str,
    allowed_signals: set[str],
) -> tuple[str, str, dict[str, str], dict[str, str], list[str]]:
    """Run one stateless, seat-authorized, no-tools Claude Code operation."""
    route = load_fable_route(seat=seat)
    if route.get("transport", "claude-code") != "claude-code":
        raise AdvisorError("Direct API transport is available only for the Advisor seat.")
    claude = resolve_claude()
    auth_mode = route.get("auth_mode", "subscription")
    auth = check_claude_auth(claude, auth_mode, route.get("api_source"))
    review_env = strict_review_environment(auth["auth_path"], auth.get("api_source"))
    api_key_helper = None
    if auth.get("api_source") == "user-settings":
        api_env, api_key_helper = user_settings_api_invocation()
        review_env.update(api_env)
    command = [
        str(claude), "-p", "--model", route["model"], "--name", REVIEW_SESSION_NAME,
        "--effort", route["effort"], "--safe-mode", "--setting-sources", "",
        "--tools", "", "--permission-mode", "dontAsk", "--no-session-persistence",
        "--prompt-suggestions", "false", "--output-format", "json",
        "--system-prompt", system_prompt,
    ]
    if auth["auth_path"] == "api":
        command.append("--bare")
    if api_key_helper is not None:
        command.extend(["--settings", json.dumps({"apiKeyHelper": api_key_helper})])
    try:
        result = subprocess.run(
            command, input=prompt, env=review_env, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdvisorError(f"Claude Fable 5 {operation} timed out; output withheld.") from exc
    except OSError as exc:
        raise AdvisorError(f"Could not start Claude Fable 5 {operation}; output withheld.") from exc
    if result.returncode != 0:
        raise AdvisorError(f"Claude Fable 5 {operation} exited with {result.returncode}; output withheld.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError(f"Claude Fable 5 {operation} returned malformed JSON.") from exc
    if isinstance(payload, list):
        completed = [
            event for event in payload
            if isinstance(event, dict) and event.get("type") == "result"
            and event.get("subtype") == "success"
        ]
        assistant_text = [
            block["text"]
            for event in payload
            if isinstance(event, dict) and isinstance(event.get("message"), dict)
            for block in event["message"].get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        payload = dict(completed[-1]) if completed else None
        if isinstance(payload, dict) and not payload.get("result") and assistant_text:
            payload["result"] = "\n".join(assistant_text)
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        raise AdvisorError(f"Claude Fable 5 {operation} returned an unexpected response.")
    response = payload["result"].strip()
    signal = _first_non_empty_line(response)
    if signal not in allowed_signals:
        expected = " or ".join(sorted(allowed_signals))
        raise AdvisorError(f"Claude Fable 5 {operation} omitted the required {expected} signal.")
    used_models = _validate_runtime_models(
        payload.get("modelUsage"), auth_path=auth["auth_path"]
    )
    return signal, response, route, auth, used_models


def _base_result(
    *, route: dict[str, str], auth: dict[str, str], used_models: list[str]
) -> dict[str, Any]:
    return {
        "model": FABLE_MODEL,
        "effort": route["effort"],
        "auth_mode": auth.get("auth_mode", "subscription"),
        "auth_path": auth.get("auth_path", "subscription"),
        "auth_method": auth["auth_method"],
        "used_models": used_models,
    }


def create_plan(packet: str) -> dict[str, Any]:
    values = _validate_inputs("plan creation", packet=packet)
    signal, response, route, auth, used_models = _invoke_fable(
        operation="plan creation", seat="planner", prompt=values["packet"],
        system_prompt=PLANNER_CREATE_SYSTEM_PROMPT, allowed_signals={"PLAN_DRAFT"},
    )
    return {"signal": signal, "plan": response, **_base_result(route=route, auth=auth, used_models=used_models)}


def _validate_revision_structure(response: str) -> None:
    lines = response.splitlines()
    ledger_positions = [i for i, line in enumerate(lines) if line.strip() == "## FINDINGS_LEDGER"]
    plan_positions = [i for i, line in enumerate(lines) if line.strip() == "## REVISED_PLAN"]
    if len(ledger_positions) != 1 or len(plan_positions) != 1:
        raise AdvisorError("Claude Fable 5 plan revision must contain exactly one FINDINGS_LEDGER and one REVISED_PLAN section.")
    ledger_index, plan_index = ledger_positions[0], plan_positions[0]
    if ledger_index >= plan_index or not "\n".join(lines[ledger_index + 1:plan_index]).strip() or not "\n".join(lines[plan_index + 1:]).strip():
        raise AdvisorError("Claude Fable 5 plan revision has an empty or misordered section.")


def revise_plan(task: str, current_plan: str, critique: str, history: str) -> dict[str, Any]:
    values = _validate_inputs("plan revision", task=task, current_plan=current_plan, critique=critique, history=history)
    prompt = "\n\n".join(
        ("# ORIGINAL_TASK\n" + values["task"], "# CANONICAL_CURRENT_PLAN_WITH_SOURCE_VERSION\n" + values["current_plan"],
         "# LATEST_ADVISOR_CRITIQUE_WITH_STABLE_FINDING_IDS\n" + values["critique"],
         "# COMPACT_CUMULATIVE_FINDINGS_HISTORY\n" + values["history"])
    )
    signal, response, route, auth, used_models = _invoke_fable(
        operation="plan revision", seat="planner", prompt=prompt,
        system_prompt=PLANNER_REVISE_SYSTEM_PROMPT, allowed_signals={"PLAN_REVISION"},
    )
    _validate_revision_structure(response)
    return {"signal": signal, "revision": response, **_base_result(route=route, auth=auth, used_models=used_models)}


def _review_plan_claude_code(
    packet: str, route: dict[str, str]
) -> dict[str, Any]:
    claude = resolve_claude()
    auth = check_claude_auth(
        claude, route["auth_mode"], route.get("api_source")
    )
    review_env = strict_review_environment(
        auth["auth_path"], auth.get("api_source")
    )
    api_key_helper = None
    if auth.get("api_source") == "user-settings":
        api_env, api_key_helper = user_settings_api_invocation()
        review_env.update(api_env)
    command = [
        str(claude),
        "-p",
        "--model",
        route["model"],
        "--name",
        REVIEW_SESSION_NAME,
        "--effort",
        route["effort"],
        "--safe-mode",
        "--setting-sources",
        "",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--prompt-suggestions",
        "false",
        "--output-format",
        "json",
        "--system-prompt",
        SYSTEM_PROMPT,
    ]
    if auth["auth_path"] == "api":
        command.append("--bare")
    if api_key_helper is not None:
        command.extend(
            ["--settings", json.dumps({"apiKeyHelper": api_key_helper})]
        )
    try:
        result = subprocess.run(
            command,
            input=packet,
            env=review_env,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdvisorError("Claude Fable 5 review timed out; output withheld.") from exc
    except OSError as exc:
        raise AdvisorError("Could not start Claude Fable 5 review; output withheld.") from exc
    if result.returncode != 0:
        raise AdvisorError(
            f"Claude Fable 5 exited with {result.returncode}; output withheld."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError("Claude Fable 5 returned malformed JSON.") from exc
    if isinstance(payload, list):
        completed = [
            event
            for event in payload
            if isinstance(event, dict)
            and event.get("type") == "result"
            and event.get("subtype") == "success"
        ]
        assistant_text = [
            block["text"]
            for event in payload
            if isinstance(event, dict) and event.get("type") == "assistant"
            for block in (
                event.get("message", {}).get("content", [])
                if isinstance(event.get("message"), dict)
                else []
            )
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        payload = dict(completed[-1]) if completed else None
        if isinstance(payload, dict) and not payload.get("result") and assistant_text:
            payload["result"] = "\n".join(assistant_text)
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        raise AdvisorError("Claude Fable 5 returned an unexpected response.")
    first, review = _normalize_review(payload["result"])
    used_models = _validate_runtime_models(
        payload.get("modelUsage"), auth_path=auth["auth_path"]
    )
    response = {
        "decision": first,
        "review": review,
        "model": FABLE_MODEL,
        "effort": route["effort"],
        "auth_mode": auth["auth_mode"],
        "auth_path": auth["auth_path"],
        "auth_method": auth["auth_method"],
        "transport": "claude-code",
        "advisor_path": "claude-code-cli",
        "used_models": used_models,
    }
    if "api_source" in auth:
        response["api_source"] = auth["api_source"]
    return response


def _review_plan_direct_api(
    packet: str, route: dict[str, str]
) -> dict[str, Any]:
    api_source = route.get("api_source")
    if api_source not in API_SOURCES:
        raise AdvisorError("Direct API transport requires an explicit API source.")
    credential_header, endpoint, auth = direct_api_configuration(api_source)
    body = {
        "model": auth["request_model"],
        "max_tokens": DIRECT_API_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": packet}],
    }
    headers = {
        "content-type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
        **credential_header,
    }
    request = urllib_request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib_request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=DIRECT_API_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise AdvisorError("Direct API returned an unsuccessful HTTP status.")
            raw = response.read()
    except urllib_error.HTTPError as exc:
        status = exc.code if isinstance(exc.code, int) else "unknown"
        exc.close()
        raise AdvisorError(f"Direct API request failed with HTTP status {status}.") from None
    except (TimeoutError, urllib_error.URLError, OSError):
        raise AdvisorError("Direct API request failed due to a network or timeout error.") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AdvisorError("Direct API returned malformed JSON.") from None
    if not isinstance(payload, dict):
        raise AdvisorError("Direct API returned an unexpected JSON value.")
    response_model = payload.get("model")
    if api_source == "config-file":
        if response_model != auth["request_model"]:
            raise AdvisorError(
                "Strict model verification failed: Python API requested model "
                f"{auth['request_model']!r} but the provider echoed {response_model!r}."
            )
    elif (
        not isinstance(response_model, str)
        or response_model not in ALLOWED_DIRECT_RESPONSE_MODELS
    ):
        raise AdvisorError(
            "Strict model verification failed: direct API returned an unapproved model echo."
        )
    stop_reason = payload.get("stop_reason")
    if stop_reason != "end_turn":
        if stop_reason == "refusal":
            details = payload.get("stop_details")
            details = details if isinstance(details, dict) else {}
            refusal_type = _safe_refusal_field(details.get("type"))
            category = _safe_refusal_field(details.get("category"))
            explanation = _safe_refusal_field(details.get("explanation"))
            raise AdvisorError(
                "Direct API response was refused; "
                f"refusal_type={refusal_type!r}; category={category!r}; "
                f"explanation={explanation!r}. Advisor unavailable; "
                "executor work must remain blocked."
            )
        raise AdvisorError(
            "Direct API response did not complete with end_turn; "
            f"stop_reason={stop_reason!r}."
        )
    content = payload.get("content")
    if not isinstance(content, list):
        raise AdvisorError("Direct API returned an unexpected response.")
    text_blocks = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    if not text_blocks:
        raise AdvisorError("Direct API returned no review text.")
    first, review = _normalize_review("\n".join(text_blocks))
    return {
        "decision": first,
        "review": review,
        "model": FABLE_MODEL,
        "request_model": auth["request_model"],
        "response_model": response_model,
        "model_echo_policy": "exact-allowlist-v1",
        "effort": "not-applied",
        "configured_effort": route["effort"],
        "auth_mode": auth["auth_mode"],
        "auth_path": auth["auth_path"],
        "auth_method": auth["auth_method"],
        "api_source": auth["api_source"],
        "transport": "direct-api",
        "advisor_path": auth["advisor_path"],
        "used_models": [FABLE_MODEL],
    }


def review_plan(packet: str) -> dict[str, Any]:
    values = _validate_inputs("plan review", packet=packet)
    route = load_fable_route()
    if route.get("transport", "claude-code") == "direct-api":
        return _review_plan_direct_api(values["packet"], route)
    return _review_plan_claude_code(values["packet"], route)


def advisor_status(route: dict[str, str]) -> dict[str, Any]:
    if route.get("transport", "claude-code") == "direct-api":
        api_source = route.get("api_source")
        if api_source not in API_SOURCES:
            raise AdvisorError("Direct API transport requires an explicit API source.")
        _, _, auth = direct_api_configuration(api_source)
        return {
            "available": True,
            **route,
            **auth,
            "effort": "not-applied",
            "configured_effort": route["effort"],
        }
    auth = check_claude_auth(
        auth_mode=route["auth_mode"], api_source=route.get("api_source")
    )
    return {
        "available": True,
        **route,
        **auth,
        "advisor_path": route.get("path", "claude-code-cli"),
    }


def _configured_fable_seats() -> dict[str, dict[str, str]]:
    payload = _read_routing_state()
    routes: dict[str, dict[str, str]] = {}
    for seat in ("planner", "advisor"):
        value = payload.get(seat)
        if value is None:
            continue
        if not isinstance(value, dict) or value.get("kind") != "fable":
            continue
        routes[seat] = _validate_fable_route(value, seat=_validate_seat(seat))
    if not routes:
        raise AdvisorError("Claude Fable 5 is not configured for Planner or Advisor.")
    return routes


def status() -> dict[str, Any]:
    routes = _configured_fable_seats()
    advisor = routes.get("advisor")
    if advisor is not None and advisor.get("transport") == "direct-api":
        auth = advisor_status(advisor)
    else:
        auth = check_claude_auth(
            auth_mode=advisor.get("auth_mode", "subscription") if advisor else "subscription",
            api_source=advisor.get("api_source") if advisor else None,
        )
    seats = {seat: {"model": route["model"], "effort": route["effort"]} for seat, route in routes.items()}
    result: dict[str, Any] = {"available": True, "configured_seats": list(seats), "seats": seats, **auth}
    if advisor is not None:
        # Preserve the legacy status surface without exposing the internal
        # subscription route label; direct-API status includes its explicit
        # transport/source metadata for diagnostics.
        result.update({"model": advisor["model"], "effort": advisor["effort"]})
        if advisor.get("transport") != "claude-code" or advisor.get("auth_mode") != "subscription":
            result.update(advisor)
    return result


def tool_definitions() -> list[dict[str, Any]]:
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    string_property = {"type": "string", "maxLength": MAX_INPUT_CHARS}
    return [
        {
            "name": "create_plan",
            "title": "Create a plan with Claude Fable 5",
            "description": "Create one stateless plan draft with the configured Fable Planner.",
            "inputSchema": {
                "type": "object",
                "properties": {"packet": {**string_property, "description": "Complete planning packet."}},
                "required": ["packet"], "additionalProperties": False,
            }, "annotations": annotations,
        },
        {
            "name": "revise_plan",
            "title": "Revise a plan with Claude Fable 5",
            "description": "Create one stateless revision with a findings ledger and complete revised plan.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {**string_property, "description": "Original task."},
                    "current_plan": {**string_property, "description": "Canonical current plan with source version."},
                    "critique": {**string_property, "description": "Latest Advisor critique with stable finding IDs."},
                    "history": {**string_property, "description": "Compact cumulative findings history."},
                },
                "required": ["task", "current_plan", "critique", "history"], "additionalProperties": False,
            }, "annotations": annotations,
        },
        {
            "name": "review_plan",
            "title": "Review a plan with Claude Fable 5",
            "description": (
                "Send one self-contained, read-only plan-review packet to the configured "
                "Claude Fable 5 advisor."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet": {
                        "type": "string",
                        "description": "Complete context, plan, risks, slices, and checks.",
                    }
                },
                "required": ["packet"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "status",
            "title": "Check Claude Fable 5 advisor status",
            "description": "Check the saved route and selected transport without a model call.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
    ]


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "isError": is_error,
    }


def _tool_arguments(arguments: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AdvisorError("Tool arguments must be an object.")
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise AdvisorError(f"Unexpected tool argument(s): {', '.join(unexpected)}.")
    return arguments


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "codex-orchestration-fable-advisor", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_definitions()}
    elif method == "tools/call":
        params = request.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        try:
            if name == "create_plan":
                args = _tool_arguments(arguments, {"packet"})
                result = _tool_result(create_plan(args.get("packet")))
            elif name == "revise_plan":
                args = _tool_arguments(arguments, {"task", "current_plan", "critique", "history"})
                result = _tool_result(revise_plan(args.get("task"), args.get("current_plan"), args.get("critique"), args.get("history")))
            elif name == "review_plan":
                args = _tool_arguments(arguments, {"packet"})
                result = _tool_result(review_plan(args.get("packet")))
            elif name == "status":
                _tool_arguments(arguments, set())
                result = _tool_result(status())
            else:
                raise AdvisorError(f"Unknown tool: {name!r}.")
        except AdvisorError as exc:
            result = _tool_result({"available": False, "error": str(exc)}, is_error=True)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = handle_request(request)
        except (json.JSONDecodeError, ValueError) as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
