#!/usr/bin/env python3
"""Read-only MCP bridge from Codex to Claude Fable 5 through Claude Code."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request
import uuid


STATE_FILENAME = ".codex-orchestration-routing.json"
FABLE_MODEL = "claude-fable-5"
SUPPORTED_EFFORTS = {"low", "medium", "high", "max"}
FIRST_PARTY_PROFILE = "first-party"
CC_SWITCH_PROFILE = "cc-switch-openrouter-loopback"
SUPPORTED_TRANSPORT_PROFILES = {FIRST_PARTY_PROFILE, CC_SWITCH_PROFILE}
CC_SWITCH_BASE_URL = "http://127.0.0.1:15721"
CC_SWITCH_HEALTH_URL = f"{CC_SWITCH_BASE_URL}/health"
OPENROUTER_FABLE_MODEL = "anthropic/claude-fable-5"
OPENROUTER_FABLE_RESPONSE_RE = re.compile(
    r"^anthropic/claude-(?:fable-5|5-fable-\d{8})$"
)
CLAUDE_TIMEOUT_SECONDS = 600
AUTH_TIMEOUT_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 3
SENSITIVE_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
}
SYSTEM_PROMPT = """You are Claude Fable 5 acting only as a plan advisor to Codex's root orchestrator.
Review the supplied self-contained packet for material correctness, missing constraints, unsafe sequencing, ownership conflicts, and verification gaps. Do not edit files, call tools, spawn agents, contact executors, or attempt implementation.

Return the structured decision and review required by the supplied JSON Schema.
Use PLAN_APPROVED only when no material gap is present. Use PLAN_REVISE when correction is needed, with a concise prioritized review in which every gap has a concrete correction. Ignore style preferences. Report only to the root orchestrator."""
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": ["PLAN_APPROVED", "PLAN_REVISE"]},
        "review": {"type": "string", "minLength": 1},
    },
    "required": ["decision", "review"],
}


class AdvisorError(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
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


def claude_settings_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "settings.json"


def cc_switch_db_path() -> Path:
    return Path.home() / ".cc-switch" / "cc-switch.db"


def _read_regular_json(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AdvisorError(f"{label} is not a regular file.")
        if info.st_nlink != 1:
            raise AdvisorError(f"{label} has multiple hard links.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AdvisorError(f"{label} is missing.") from exc
    except AdvisorError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdvisorError(f"Could not read {label}.") from exc
    if not isinstance(payload, dict):
        raise AdvisorError(f"{label} has an unexpected JSON value.")
    return payload


def _normalized_model(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\[[0-9]+[KMG]\]$", "", value.strip(), flags=re.IGNORECASE)


def _open_cc_switch_database(path: Path | None = None) -> sqlite3.Connection:
    database = (path or cc_switch_db_path()).expanduser()
    try:
        info = database.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AdvisorError("CC Switch database is not a regular file.")
        uri = f"{database.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
    except FileNotFoundError as exc:
        raise AdvisorError("CC Switch database is missing.") from exc
    except AdvisorError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise AdvisorError("Could not open the CC Switch database read-only.") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _check_cc_switch_health() -> None:
    request = urllib.request.Request(
        CC_SWITCH_HEALTH_URL,
        headers={"Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=HEALTH_TIMEOUT_SECONDS) as response:
            body = response.read(4097)
            status = response.status
            final_url = response.geturl()
    except (OSError, urllib.error.URLError) as exc:
        raise AdvisorError("CC Switch loopback health check failed.") from exc
    if status != 200 or final_url != CC_SWITCH_HEALTH_URL or len(body) > 4096:
        raise AdvisorError("CC Switch loopback health check failed.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdvisorError("CC Switch returned malformed health JSON.") from exc
    if not isinstance(payload, dict) or payload.get("status") != "healthy":
        raise AdvisorError("CC Switch is not healthy.")


def inspect_cc_switch_route() -> dict[str, str]:
    query = """
        SELECT p.name AS provider_name,
               json_extract(
                   p.settings_config,
                   '$.env.ANTHROPIC_DEFAULT_FABLE_MODEL'
               ) AS fable_model,
               c.proxy_enabled,
               c.listen_address,
               c.listen_port,
               c.enabled
        FROM providers AS p
        JOIN proxy_config AS c ON c.app_type = 'claude'
        WHERE p.app_type = 'claude' AND p.is_current = 1
    """
    try:
        with closing(_open_cc_switch_database()) as connection:
            rows = connection.execute(query).fetchall()
    except sqlite3.Error as exc:
        raise AdvisorError("Could not inspect the CC Switch route.") from exc
    if len(rows) != 1:
        raise AdvisorError("CC Switch must have exactly one current Claude provider.")
    row = rows[0]
    if str(row["provider_name"]).casefold() != "openrouter":
        raise AdvisorError("CC Switch current Claude provider is not OpenRouter.")
    if not (
        row["proxy_enabled"] == 1
        and row["enabled"] == 1
        and row["listen_address"] == "127.0.0.1"
        and row["listen_port"] == 15721
    ):
        raise AdvisorError("CC Switch Claude loopback configuration is unavailable.")
    mapped = _normalized_model(row["fable_model"])
    if mapped != OPENROUTER_FABLE_MODEL:
        raise AdvisorError("CC Switch OpenRouter Fable mapping is not Claude Fable 5.")
    return {"provider": "OpenRouter", "mapped_model": mapped}


def verify_cc_switch_prerequisites() -> dict[str, str]:
    settings = _read_regular_json(claude_settings_path(), "Claude user settings")
    env = settings.get("env")
    if not isinstance(env, dict):
        raise AdvisorError("Claude user settings have no environment configuration.")
    if env.get("ANTHROPIC_BASE_URL") != CC_SWITCH_BASE_URL:
        raise AdvisorError("Claude user settings do not target the CC Switch loopback.")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(token, str) or not token:
        raise AdvisorError("Claude user settings have no CC Switch auth token.")
    if env.get("ANTHROPIC_API_KEY") not in {None, ""}:
        raise AdvisorError("Claude user settings contain a conflicting Anthropic API key.")
    _check_cc_switch_health()
    return inspect_cc_switch_route()


def cc_switch_log_cursor() -> int:
    try:
        with closing(_open_cc_switch_database()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) AS cursor FROM proxy_request_logs"
            ).fetchone()
    except sqlite3.Error as exc:
        raise AdvisorError("Could not read the CC Switch request cursor.") from exc
    return int(row["cursor"])


def confirm_cc_switch_request(cursor: int, session_id: str) -> dict[str, Any]:
    query = """
        SELECT p.name AS provider_name,
               l.request_model,
               l.model,
               l.status_code
        FROM proxy_request_logs AS l
        JOIN providers AS p
          ON p.id = l.provider_id AND p.app_type = l.app_type
        WHERE l.rowid > ? AND l.app_type = 'claude' AND l.session_id = ?
        ORDER BY l.rowid
    """
    try:
        with closing(_open_cc_switch_database()) as connection:
            rows = connection.execute(query, (cursor, session_id)).fetchall()
    except sqlite3.Error as exc:
        raise AdvisorError("Could not confirm the CC Switch Fable request.") from exc
    fable_rows = [
        row
        for row in rows
        if _normalized_model(row["request_model"])
        in {FABLE_MODEL, OPENROUTER_FABLE_MODEL}
    ]
    if len(fable_rows) != 1:
        raise AdvisorError(
            "Runtime evidence did not contain one unambiguous fresh CC Switch "
            "OpenRouter Fable request."
        )
    row = fable_rows[0]
    if not (
        str(row["provider_name"]).casefold() == "openrouter"
        and isinstance(row["model"], str)
        and OPENROUTER_FABLE_RESPONSE_RE.fullmatch(row["model"])
        and row["status_code"] == 200
    ):
        raise AdvisorError(
            "The fresh CC Switch request did not confirm the OpenRouter Fable route."
        )
    return {
        "provider": "OpenRouter",
        "request_model": _normalized_model(row["request_model"]),
        "model": row["model"],
        "status_code": 200,
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


def check_claude_auth(
    claude: Path | None = None,
    transport_profile: str = FIRST_PARTY_PROFILE,
) -> dict[str, str]:
    executable = claude or resolve_claude()
    if transport_profile not in SUPPORTED_TRANSPORT_PROFILES:
        raise AdvisorError("Unsupported Claude Fable 5 transport profile.")
    gateway: dict[str, str] | None = None
    if transport_profile == CC_SWITCH_PROFILE:
        gateway = verify_cc_switch_prerequisites()
    payload = _run_json([str(executable), "auth", "status"], timeout=AUTH_TIMEOUT_SECONDS)
    if transport_profile == CC_SWITCH_PROFILE:
        if not (
            payload.get("loggedIn") is True
            and payload.get("authMethod") in {"oauth_token", "api_key"}
        ):
            raise AdvisorError(
                "Claude Code is not authenticated through the CC Switch/OpenRouter route."
            )
        return {
            "auth_method": "gateway",
            "api_provider": gateway["provider"],
        }
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
    return {
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
    if model != FABLE_MODEL or effort not in SUPPORTED_EFFORTS:
        raise AdvisorError("The saved Claude Fable 5 route is invalid.")
    transport = route.get("transport")
    if payload.get("schema") == 3 and transport is None:
        raise AdvisorError("The saved Claude Fable 5 transport is missing.")
    if transport is None:
        profile = FIRST_PARTY_PROFILE
    elif (
        isinstance(transport, dict)
        and transport.get("kind") == "claude-code"
        and transport.get("profile") in SUPPORTED_TRANSPORT_PROFILES
        and set(transport) == {"kind", "profile"}
    ):
        profile = transport["profile"]
    else:
        raise AdvisorError("The saved Claude Fable 5 transport is invalid.")
    return {"model": model, "effort": effort, "transport_profile": profile}


def review_plan(packet: str) -> dict[str, Any]:
    if not isinstance(packet, str) or not packet.strip():
        raise AdvisorError("`packet` must be a non-empty self-contained review packet.")
    route = load_fable_route()
    claude = resolve_claude()
    profile = route["transport_profile"]
    auth = check_claude_auth(claude, profile)
    cursor = cc_switch_log_cursor() if profile == CC_SWITCH_PROFILE else None
    session_id = str(uuid.uuid4())
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
        "--session-id",
        session_id,
        "--prompt-suggestions",
        "false",
        "--setting-sources",
        "user",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(REVIEW_SCHEMA, separators=(",", ":")),
        "--system-prompt",
        SYSTEM_PROMPT,
    ]
    try:
        result = subprocess.run(
            command,
            input=packet,
            env=sanitized_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CLAUDE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdvisorError(f"Claude Fable 5 review failed: {exc}") from exc
    if result.returncode != 0:
        raise AdvisorError(
            f"Claude Fable 5 exited with {result.returncode}; "
            "inspect Claude Code and CC Switch logs locally."
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AdvisorError("Claude Fable 5 returned malformed JSON.") from exc
    if not isinstance(payload, dict):
        raise AdvisorError("Claude Fable 5 returned an unexpected response.")
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        raise AdvisorError("Claude Fable 5 omitted the required structured review.")
    decision = structured.get("decision")
    review_body = structured.get("review")
    if decision not in {"PLAN_APPROVED", "PLAN_REVISE"}:
        raise AdvisorError("Claude Fable 5 omitted the required plan decision.")
    if not isinstance(review_body, str) or not review_body.strip():
        raise AdvisorError("Claude Fable 5 omitted the required review text.")
    review = f"{decision}\n{review_body.strip()}"
    usage = payload.get("modelUsage")
    used_models = sorted(usage) if isinstance(usage, dict) else []
    if FABLE_MODEL not in used_models:
        raise AdvisorError("Runtime metadata did not confirm Claude Fable 5.")
    response = {
        "decision": decision,
        "review": review,
        "model": FABLE_MODEL,
        "effort": route["effort"],
        "auth_method": auth["auth_method"],
        "used_models": used_models,
        "transport_profile": profile,
    }
    if cursor is not None:
        response["route_confirmation"] = confirm_cc_switch_request(cursor, session_id)
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
                    transport_profile=route["transport_profile"]
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
