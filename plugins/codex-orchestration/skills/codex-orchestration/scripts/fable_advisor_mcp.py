#!/usr/bin/env python3
"""Read-only MCP bridge from Codex to Claude Fable 5 through Claude Code."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


STATE_FILENAME = ".codex-orchestration-routing.json"
FABLE_MODEL = "claude-fable-5"
ALLOWED_MODELS = frozenset({FABLE_MODEL})
REVIEW_SESSION_NAME = "codex-fable-review"
SUPPORTED_EFFORTS = {"low", "medium", "high", "max"}
AUTH_MODES = {"subscription", "api", "auto"}
API_SOURCES = {"environment", "user-settings"}
CLAUDE_TIMEOUT_SECONDS = 600
AUTH_TIMEOUT_SECONDS = 20
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
SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a plan advisor to Codex's root orchestrator.
Review the supplied self-contained packet for material correctness, missing constraints, unsafe sequencing, ownership conflicts, and verification gaps. Do not edit files, call tools, spawn agents, contact executors, or attempt implementation.

Your first non-empty line must be exactly PLAN_APPROVED or PLAN_REVISE.
Use PLAN_APPROVED only when no material gap is present. Use PLAN_REVISE when correction is needed, followed by a concise prioritized list in which every gap has a concrete correction. Ignore style preferences. Report only to the root orchestrator."""


class AdvisorError(RuntimeError):
    pass


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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdvisorError(f"Could not run Claude Code: {exc}") from exc
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
    executable = claude or resolve_claude()
    if api_source is not None and api_source not in API_SOURCES:
        raise AdvisorError(f"Unsupported Claude API source: {api_source!r}.")
    if auth_mode != "api" and api_source is not None:
        raise AdvisorError("Claude API source is valid only in api authentication mode.")
    api_sources = set(api_credential_sources())
    route_sources = set(api_route_sources())
    if auth_mode == "auto" and route_sources:
        raise AdvisorError(
            "Claude API/Gateway configuration is present; choose api mode and an API source "
            "explicitly instead of allowing auto to select a potentially metered path."
        )
    auth_path = "subscription" if auth_mode == "auto" else auth_mode
    if auth_path == "api":
        if api_source is None:
            raise AdvisorError(
                "Claude api mode requires an explicit API source: environment or user-settings."
            )
        if api_source not in api_sources:
            raise AdvisorError(
                f"Claude API credentials are not configured in the saved {api_source} source."
            )
        conflicting_sources = route_sources - {api_source}
        if conflicting_sources:
            raise AdvisorError(
                "Claude API/Gateway configuration exists in more than the saved source; "
                "remove the conflicting source before using api mode."
            )
        return {
            "auth_mode": auth_mode,
            "auth_path": "api",
            "auth_method": "api",
            "api_source": api_source,
        }
    if route_sources:
        raise AdvisorError(
            "Claude API/Gateway configuration is present, so subscription mode cannot prove that the "
            "model call will use subscription billing; choose api mode and an API source "
            "explicitly, or remove the API configuration."
        )

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


def load_fable_route(home: Path | None = None) -> dict[str, str]:
    path = (home or codex_home()) / STATE_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdvisorError("Claude Fable 5 is not configured; run setup first.") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdvisorError(f"Could not read the routing state: {exc}") from exc
    route = payload.get("advisor") if isinstance(payload, dict) else None
    if not isinstance(route, dict) or route.get("kind") != "fable":
        raise AdvisorError("Claude Fable 5 is not the configured advisor.")
    model = route.get("model")
    effort = route.get("effort")
    auth_mode = route.get("auth_mode", "subscription")
    api_source = route.get("api_source")
    if model != FABLE_MODEL or effort not in SUPPORTED_EFFORTS:
        raise AdvisorError("The saved Claude Fable 5 route is invalid.")
    if auth_mode not in AUTH_MODES:
        raise AdvisorError("The saved Claude Fable 5 auth mode is invalid.")
    if auth_mode == "api" and api_source not in API_SOURCES:
        raise AdvisorError("The saved Claude Fable 5 API source is invalid.")
    if auth_mode != "api" and api_source is not None:
        raise AdvisorError("The saved Claude Fable 5 API source is unexpected.")
    result = {"model": model, "effort": effort, "auth_mode": auth_mode}
    if api_source is not None:
        result["api_source"] = api_source
    return result


def review_plan(packet: str) -> dict[str, Any]:
    if not isinstance(packet, str) or not packet.strip():
        raise AdvisorError("`packet` must be a non-empty self-contained review packet.")
    route = load_fable_route()
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdvisorError(f"Claude Fable 5 review failed: {exc}") from exc
    if result.returncode != 0:
        raise AdvisorError(
            f"Claude Fable 5 exited with {result.returncode}; inspect Claude Code "
            "diagnostics locally."
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
    review = payload["result"].strip()
    if not review:
        raise AdvisorError("Claude Fable 5 returned an empty review.")
    first = next((line.strip() for line in review.splitlines() if line.strip()), "")
    if first not in {"PLAN_APPROVED", "PLAN_REVISE"}:
        first = "PLAN_REVISE"
        review = f"{first}\n\n{review}"
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        raise AdvisorError(
            "Strict model verification failed: Claude Code omitted modelUsage metadata."
        )
    used_models = sorted(set(usage))
    if set(used_models) != ALLOWED_MODELS:
        raise AdvisorError(
            "Strict model verification failed: expected only claude-fable-5, got "
            f"{used_models!r}. Advisor unavailable; executor work must remain blocked."
        )
    response = {
        "decision": first,
        "review": review,
        "model": FABLE_MODEL,
        "effort": route["effort"],
        "auth_mode": auth["auth_mode"],
        "auth_path": auth["auth_path"],
        "auth_method": auth["auth_method"],
        "used_models": used_models,
    }
    if "api_source" in auth:
        response["api_source"] = auth["api_source"]
    return response


def tool_definitions() -> list[dict[str, Any]]:
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    return [
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
            "description": "Check the saved route and Claude Code login without a model call.",
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
            if name == "review_plan":
                packet = arguments.get("packet") if isinstance(arguments, dict) else None
                result = _tool_result(review_plan(packet))
            elif name == "status":
                route = load_fable_route()
                auth = check_claude_auth(
                    auth_mode=route["auth_mode"],
                    api_source=route.get("api_source"),
                )
                result = _tool_result({"available": True, **route, **auth})
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
