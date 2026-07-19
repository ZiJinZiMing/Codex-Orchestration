#!/usr/bin/env python3
"""Root-directed, no-tools MCP bridge from Codex to Claude Fable 5.

The managed policy reserves stateless Planner and Advisor operations for the
root; MCP requests do not carry caller identity, so the server cannot enforce
that caller boundary. Each model call reloads and authorizes its seat from
routing state, rechecks first-party Claude Code authentication, and uses a fresh
no-tools/no-persistence process.
"""

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

from configure_fable_api import FableApiConfigError, load_config as load_fable_api_config
import routing_state


STATE_FILENAME = ".codex-orchestration-routing.json"
MANAGED_MARKER = routing_state.MANAGED_MARKER
FABLE_MODEL = routing_state.FABLE_MODEL
FABLE_SERVERS = routing_state.FABLE_SERVERS
SUPPORTED_EFFORTS = routing_state.FABLE_EFFORTS
# Claude Code currently reports this exact internal helper alongside Fable for
# some calls. Keep the runtime policy explicit and fail closed if that identity
# rotates or any other model appears.
FABLE_HELPER_MODEL = "claude-haiku-4-5-20251001"
ALLOWED_RUNTIME_MODELS = frozenset({FABLE_MODEL, FABLE_HELPER_MODEL})
CLAUDE_TIMEOUT_SECONDS = 600
AUTH_TIMEOUT_SECONDS = 20
DIRECT_API_TIMEOUT_SECONDS = 600
DIRECT_API_MAX_TOKENS = 65536
DIRECT_API_MAX_RESPONSE_BYTES = 2_000_000
ANTHROPIC_VERSION = "2023-06-01"
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Applies to the combined user-controlled text sent by one model operation.
MAX_INPUT_CHARS = 200_000
SENSITIVE_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
}
STALE_BRIDGE_RECOVERY = (
    "If Codex Orchestration changed after this task started, run fresh native status. "
    "When status reports first-party login ready, fully quit and reopen Codex and "
    "start a new task; do not re-authenticate solely for this loaded-bridge failure."
)

ADVISOR_SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a plan advisor to Codex's root orchestrator.
Review the supplied self-contained packet for material correctness, missing constraints, unsafe sequencing, ownership conflicts, and verification gaps. Do not edit files, call tools, spawn agents, contact the Planner or executors, or attempt implementation.

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
    """Fail-closed error for any Fable bridge operation."""


def _safe_refusal_field(value: Any, max_chars: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:max_chars] if cleaned else None


def _safe_http_error_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 96:
        return None
    if not all(char.isascii() and (char.isalnum() or char in "._-") for char in value):
        return None
    return value


def _safe_http_error_diagnostics(exc: urllib_error.HTTPError) -> str:
    """Expose only bounded provider type and retry timing, never response text."""

    details: list[str] = []
    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
    if (
        isinstance(retry_after, str)
        and retry_after.strip().isdigit()
        and len(retry_after.strip()) <= 10
    ):
        details.append(f"retry_after_seconds={retry_after.strip()}")
    try:
        payload = json.loads(exc.read(4096).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        error_type = _safe_http_error_type(
            error.get("type") if isinstance(error, dict) else None
        )
        if error_type is not None:
            details.append(f"provider_error_type={error_type}")
    return "; " + "; ".join(details) if details else ""


class NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Prevent forwarding the configured API credential across redirects."""

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


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser() if value else Path.home() / ".codex"


def sanitized_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in SENSITIVE_ENV:
        env.pop(name, None)
    return env


def _python_api_configuration(home: Path | None = None) -> tuple[dict[str, str], str, str]:
    try:
        config = load_fable_api_config(codex_home=home or codex_home())
    except FableApiConfigError as exc:
        raise AdvisorError(str(exc)) from exc
    if not config["enabled"]:
        raise AdvisorError(
            "Python API Advisor is disabled because the provider api_key is empty."
        )
    provider = config["provider"]
    credential = (
        {"Authorization": f"Bearer {provider['api_key']}"}
        if provider["auth_type"] == "bearer"
        else {"x-api-key": provider["api_key"]}
    )
    return credential, provider["api_url"], provider["model"]


def check_python_api_config(home: Path | None = None) -> dict[str, str]:
    """Validate the configured Python API Advisor without exposing its secret."""

    _, endpoint, request_model = _python_api_configuration(home)
    return {
        "auth_method": "config-file",
        "api_source": routing_state.FABLE_API_SOURCE,
        "transport": routing_state.FABLE_API_TRANSPORT,
        "advisor_path": routing_state.FABLE_API_PATH,
        "api_url": endpoint,
        "request_model": request_model,
    }


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


def _run_json(command: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            env=sanitized_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdvisorError("Claude Code authentication check timed out.") from exc
    except OSError as exc:
        raise AdvisorError("Could not run Claude Code authentication check.") from exc
    if result.returncode != 0:
        raise AdvisorError(
            f"Claude Code authentication check exited with {result.returncode}; output withheld."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError("Claude Code returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise AdvisorError("Claude Code returned an unexpected JSON value.")
    return payload


def check_claude_auth(claude: Path | None = None) -> dict[str, str]:
    executable = claude or resolve_claude()
    payload = _run_json([str(executable), "auth", "status"], timeout=AUTH_TIMEOUT_SECONDS)
    subscription = payload.get("subscriptionType")
    if not (
        payload.get("loggedIn") is True
        and payload.get("authMethod") == "claude.ai"
        and payload.get("apiProvider") == "firstParty"
        and subscription in {"pro", "max"}
    ):
        raise AdvisorError(
            "Claude Code must be logged in through a first-party Pro or Max account; "
            "run `claude auth login` and try again."
        )
    return {"auth_method": "claude.ai", "api_provider": "firstParty"}


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
    try:
        state = routing_state.validate_routing_state(payload)
    except routing_state.RoutingStateError as exc:
        raise AdvisorError("The saved routing state is invalid.") from exc
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
    return state


def _validate_seat(seat: str) -> Seat:
    if seat not in {"planner", "advisor"}:
        raise AdvisorError("Fable seat must be `planner` or `advisor`.")
    return seat  # type: ignore[return-value]


def _validate_fable_route(route: Any, *, seat: Seat) -> dict[str, str]:
    if not isinstance(route, dict) or route.get("kind") != "fable":
        raise AdvisorError(f"Claude Fable 5 is not the configured {seat}.")
    result = {"model": route["model"], "effort": route["effort"]}
    if seat == "advisor" and route.get("transport") == routing_state.FABLE_API_TRANSPORT:
        if (
            route.get("api_source") != routing_state.FABLE_API_SOURCE
            or route.get("path") != routing_state.FABLE_API_PATH
        ):
            raise AdvisorError("The saved Python API Advisor route is invalid.")
        result.update(
            {
                "transport": routing_state.FABLE_API_TRANSPORT,
                "api_source": routing_state.FABLE_API_SOURCE,
                "path": routing_state.FABLE_API_PATH,
            }
        )
    return result


def load_fable_route(
    home: Path | None = None, *, seat: str = "advisor"
) -> dict[str, str]:
    """Load and validate one explicitly authorized Fable seat.

    ``seat`` defaults to Advisor for compatibility with the original bridge.
    It is deliberately constrained and resolved from disk on every invocation.
    """

    selected = _validate_seat(seat)
    payload = _read_routing_state(home)
    return _validate_fable_route(payload.get(selected), seat=selected)


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


def _validate_runtime_models(usage: Any) -> list[str]:
    raw_models = list(usage) if isinstance(usage, dict) else []
    if not all(isinstance(model, str) for model in raw_models):
        raise AdvisorError(
            "Runtime metadata reported a model outside the allowed Fable runtime policy."
        )
    used_models = sorted(raw_models)
    if FABLE_MODEL not in used_models:
        raise AdvisorError(
            "Runtime metadata did not confirm the pinned Claude Fable 5 primary model."
        )
    if not set(used_models).issubset(ALLOWED_RUNTIME_MODELS):
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
    """Run one stateless, seat-authorized, no-tools Fable operation."""

    route = load_fable_route(seat=seat)
    claude = resolve_claude()
    auth = check_claude_auth(claude)
    command = [
        str(claude),
        "-p",
        "--model",
        route["model"],
        "--effort",
        route["effort"],
        "--safe-mode",
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
        system_prompt,
    ]
    try:
        result = subprocess.run(
            command,
            input=prompt,
            env=sanitized_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdvisorError(f"Claude Fable 5 {operation} timed out.") from exc
    except OSError as exc:
        raise AdvisorError(f"Could not start Claude Fable 5 {operation}.") from exc
    if result.returncode != 0:
        raise AdvisorError(
            f"Claude Fable 5 {operation} exited with {result.returncode}; output withheld."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError(f"Claude Fable 5 {operation} returned malformed JSON.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        raise AdvisorError(f"Claude Fable 5 {operation} returned an unexpected response.")
    # Authorize the complete runtime identity set before interpreting or
    # returning any model-authored plan/review content.
    used_models = _validate_runtime_models(payload.get("modelUsage"))
    response = payload["result"].strip()
    signal = _first_non_empty_line(response)
    if signal not in allowed_signals:
        if operation == "plan review":
            raise AdvisorError("Claude Fable 5 omitted the required plan decision.")
        expected = " or ".join(sorted(allowed_signals))
        raise AdvisorError(
            f"Claude Fable 5 {operation} omitted the required {expected} signal."
        )
    return signal, response, route, auth, used_models


def _base_result(
    *, route: dict[str, str], auth: dict[str, str], used_models: list[str]
) -> dict[str, Any]:
    return {
        # ``model`` is the route's pinned primary identity; ``used_models``
        # preserves every runtime-reported model, including an allowed helper.
        "model": FABLE_MODEL,
        "effort": route["effort"],
        "auth_method": auth["auth_method"],
        "used_models": used_models,
    }


def create_plan(packet: str) -> dict[str, Any]:
    values = _validate_inputs("plan creation", packet=packet)
    signal, response, route, auth, used_models = _invoke_fable(
        operation="plan creation",
        seat="planner",
        prompt=values["packet"],
        system_prompt=PLANNER_CREATE_SYSTEM_PROMPT,
        allowed_signals={"PLAN_DRAFT"},
    )
    return {
        "signal": signal,
        "plan": response,
        **_base_result(route=route, auth=auth, used_models=used_models),
    }


def _validate_revision_structure(response: str) -> None:
    lines = response.splitlines()
    ledger_positions = [
        i for i, line in enumerate(lines) if line.strip() == "## FINDINGS_LEDGER"
    ]
    plan_positions = [
        i for i, line in enumerate(lines) if line.strip() == "## REVISED_PLAN"
    ]
    if len(ledger_positions) != 1 or len(plan_positions) != 1:
        raise AdvisorError(
            "Claude Fable 5 plan revision must contain exactly one FINDINGS_LEDGER "
            "and one REVISED_PLAN section."
        )
    ledger_index = ledger_positions[0]
    plan_index = plan_positions[0]
    if ledger_index >= plan_index:
        raise AdvisorError(
            "Claude Fable 5 plan revision sections are in the wrong order."
        )
    ledger = "\n".join(lines[ledger_index + 1 : plan_index]).strip()
    revised_plan = "\n".join(lines[plan_index + 1 :]).strip()
    if not ledger or not revised_plan:
        raise AdvisorError(
            "Claude Fable 5 plan revision has an empty FINDINGS_LEDGER or REVISED_PLAN section."
        )


def revise_plan(
    task: str, current_plan: str, critique: str, history: str
) -> dict[str, Any]:
    values = _validate_inputs(
        "plan revision",
        task=task,
        current_plan=current_plan,
        critique=critique,
        history=history,
    )
    prompt = "\n\n".join(
        (
            "# ORIGINAL_TASK\n" + values["task"],
            "# CANONICAL_CURRENT_PLAN_WITH_SOURCE_VERSION\n" + values["current_plan"],
            "# LATEST_ADVISOR_CRITIQUE_WITH_STABLE_FINDING_IDS\n" + values["critique"],
            "# COMPACT_CUMULATIVE_FINDINGS_HISTORY\n" + values["history"],
        )
    )
    signal, response, route, auth, used_models = _invoke_fable(
        operation="plan revision",
        seat="planner",
        prompt=prompt,
        system_prompt=PLANNER_REVISE_SYSTEM_PROMPT,
        allowed_signals={"PLAN_REVISION"},
    )
    _validate_revision_structure(response)
    return {
        "signal": signal,
        "revision": response,
        **_base_result(route=route, auth=auth, used_models=used_models),
    }


def _review_plan_python_api(packet: str, route: dict[str, str]) -> dict[str, Any]:
    credential_header, endpoint, request_model = _python_api_configuration()
    body = {
        "model": request_model,
        "max_tokens": DIRECT_API_MAX_TOKENS,
        "system": ADVISOR_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": packet}],
    }
    request = urllib_request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            **credential_header,
        },
        method="POST",
    )
    handlers: list[Any] = [NoRedirectHandler()]
    if urllib_parse.urlsplit(endpoint).hostname in LOCAL_HTTP_HOSTS:
        handlers.insert(0, urllib_request.ProxyHandler({}))
    opener = urllib_request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=DIRECT_API_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise AdvisorError("Python API returned an unsuccessful HTTP status.")
            raw = response.read(DIRECT_API_MAX_RESPONSE_BYTES + 1)
            if len(raw) > DIRECT_API_MAX_RESPONSE_BYTES:
                raise AdvisorError("Python API response exceeded the bounded size limit.")
    except urllib_error.HTTPError as exc:
        status = exc.code if isinstance(exc.code, int) else "unknown"
        diagnostics = _safe_http_error_diagnostics(exc)
        exc.close()
        raise AdvisorError(
            f"Python API request failed with HTTP status {status}.{diagnostics}"
        ) from None
    except (TimeoutError, urllib_error.URLError, OSError):
        raise AdvisorError(
            "Python API request failed due to a network or timeout error."
        ) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AdvisorError("Python API returned malformed JSON.") from None
    if not isinstance(payload, dict):
        raise AdvisorError("Python API returned an unexpected JSON value.")
    response_model = payload.get("model")
    if response_model != request_model:
        raise AdvisorError(
            "Strict model verification failed: Python API requested model "
            f"{request_model!r} but the provider echoed {response_model!r}."
        )
    stop_reason = payload.get("stop_reason")
    if stop_reason != "end_turn":
        if stop_reason == "refusal":
            details = payload.get("stop_details")
            details = details if isinstance(details, dict) else {}
            raise AdvisorError(
                "Python API response was refused; "
                f"refusal_type={_safe_refusal_field(details.get('type'))!r}; "
                f"category={_safe_refusal_field(details.get('category'))!r}. "
                "Advisor unavailable; executor work must remain blocked."
            )
        raise AdvisorError(
            "Python API response did not complete with end_turn; "
            f"stop_reason={stop_reason!r}."
        )
    content = payload.get("content")
    if not isinstance(content, list):
        raise AdvisorError("Python API returned an unexpected response.")
    text_blocks = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    if not text_blocks:
        raise AdvisorError("Python API returned no review text.")
    review = "\n".join(text_blocks).strip()
    decision = _first_non_empty_line(review)
    if decision not in {"PLAN_APPROVED", "PLAN_REVISE"}:
        raise AdvisorError("Claude Fable 5 omitted the required plan decision.")
    return {
        "decision": decision,
        "review": review,
        "model": FABLE_MODEL,
        "request_model": request_model,
        "response_model": response_model,
        "effort": "not-applied",
        "configured_effort": route["effort"],
        "auth_method": "config-file",
        "api_source": routing_state.FABLE_API_SOURCE,
        "transport": routing_state.FABLE_API_TRANSPORT,
        "advisor_path": routing_state.FABLE_API_PATH,
        "used_models": [FABLE_MODEL],
    }


def review_plan(packet: str) -> dict[str, Any]:
    values = _validate_inputs("plan review", packet=packet)
    route = load_fable_route(seat="advisor")
    if route.get("transport") == routing_state.FABLE_API_TRANSPORT:
        return _review_plan_python_api(values["packet"], route)
    signal, response, route, auth, used_models = _invoke_fable(
        operation="plan review",
        seat="advisor",
        prompt=values["packet"],
        system_prompt=ADVISOR_SYSTEM_PROMPT,
        allowed_signals={"PLAN_APPROVED", "PLAN_REVISE"},
    )
    return {
        "decision": signal,
        "review": response,
        **_base_result(route=route, auth=auth, used_models=used_models),
    }


def _configured_fable_seats() -> dict[str, dict[str, str]]:
    payload = _read_routing_state()
    routes: dict[str, dict[str, str]] = {}
    for seat in ("planner", "advisor"):
        value = payload.get(seat)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise AdvisorError(f"The saved {seat} route is invalid.")
        if value.get("kind") != "fable":
            continue
        routes[seat] = _validate_fable_route(value, seat=_validate_seat(seat))
    if not routes:
        raise AdvisorError("Claude Fable 5 is not configured for Planner or Advisor.")
    return routes


def status() -> dict[str, Any]:
    routes = _configured_fable_seats()
    advisor = routes.get("advisor")
    if advisor is not None and advisor.get("transport") == routing_state.FABLE_API_TRANSPORT:
        api_status = check_python_api_config()
        auth: dict[str, Any] = {
            **api_status,
            "model_call": False,
        }
    else:
        auth = check_claude_auth()
    seats = {
        seat: {"model": route["model"], "effort": route["effort"]}
        for seat, route in routes.items()
    }
    result: dict[str, Any] = {
        "available": True,
        "configured_seats": list(seats),
        "seats": seats,
        **auth,
    }
    # Preserve the unambiguous legacy Advisor status fields.
    if "advisor" in seats:
        result.update(seats["advisor"])
        if advisor is not None and advisor.get("transport") == routing_state.FABLE_API_TRANSPORT:
            result["effort"] = "not-applied"
            result["configured_effort"] = advisor["effort"]
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
                "required": ["packet"],
                "additionalProperties": False,
            },
            "annotations": annotations,
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
                "required": ["task", "current_plan", "critique", "history"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "review_plan",
            "title": "Review a plan with Claude Fable 5",
            "description": "Review one self-contained packet with the configured Fable Advisor.",
            "inputSchema": {
                "type": "object",
                "properties": {"packet": {**string_property, "description": "Complete context, plan, risks, slices, and checks."}},
                "required": ["packet"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "status",
            "title": "Check Claude Fable 5 Planner and Advisor status",
            "description": "Check configured Fable seats and first-party login without a model call.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
            "serverInfo": {"name": "codex-orchestration-fable-advisor", "version": "2.0.0"},
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
                result = _tool_result(
                    revise_plan(
                        args.get("task"),
                        args.get("current_plan"),
                        args.get("critique"),
                        args.get("history"),
                    )
                )
            elif name == "review_plan":
                args = _tool_arguments(arguments, {"packet"})
                result = _tool_result(review_plan(args.get("packet")))
            elif name == "status":
                _tool_arguments(arguments, set())
                result = _tool_result(status())
            else:
                raise AdvisorError(f"Unknown tool: {name!r}.")
        except AdvisorError as exc:
            result = _tool_result(
                {
                    "available": False,
                    "error": str(exc),
                    "recovery": STALE_BRIDGE_RECOVERY,
                },
                is_error=True,
            )
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
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
