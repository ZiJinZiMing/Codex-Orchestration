#!/usr/bin/env python3
"""Root-directed, no-tools MCP bridge for one configured API Designer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from configure_designer_api import (
    DesignerApiConfigError,
    config_sha256,
    endpoint_sha256,
    load_config,
)
import routing_state


STATE_FILENAME = ".codex-orchestration-routing.json"
ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT_SECONDS = 600
MAX_RESPONSE_BYTES = 2_000_000
MAX_INPUT_CHARS = 200_000
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
STALE_BRIDGE_RECOVERY = (
    "If Codex Orchestration changed after this task started, run fresh native status, "
    "fully quit and reopen Codex, and start a new task."
)

DESIGNER_SYSTEM_PROMPT = """You are the configured Designer reporting only to Codex's root orchestrator.
Produce the bounded visual, UX, interaction, information-architecture, or design-system handoff requested in the packet. Do not call tools, edit files, persist a session, contact Planner, Advisor, or Executor, revise the canonical plan, change implementation code, or release Executor work.

Your first non-empty line must be exactly DESIGN_COMPLETE. Follow it with a non-empty, implementation-ready design handoff. Report only to the root orchestrator."""


class DesignerError(RuntimeError):
    """Fail-closed error for the direct-API Designer bridge."""


class NoRedirectHandler(urllib_request.HTTPRedirectHandler):
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
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _safe_field(value: Any, limit: int = 96) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if not all(char.isascii() and (char.isalnum() or char in "._-") for char in value):
        return None
    return value


def _safe_http_error_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
        return None
    return value


def _safe_http_error_diagnostics(exc: urllib_error.HTTPError) -> str:
    details: list[str] = []
    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
    if isinstance(retry_after, str) and retry_after.strip().isdigit() and len(retry_after.strip()) <= 10:
        details.append(f"retry_after_seconds={retry_after.strip()}")
    try:
        payload = json.loads(exc.read(4096).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            candidate = (
                error["error_type"]
                if "error_type" in error
                else error.get("type")
            )
            error_type = _safe_http_error_type(candidate)
        else:
            error_type = None
        if error_type is not None:
            details.append(f"provider_error_type={error_type}")
    return "; " + "; ".join(details) if details else ""


def _read_routing_state(home: Path | None = None) -> dict[str, Any]:
    root = home or codex_home()
    path = root / STATE_FILENAME
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise DesignerError("The saved routing state is not a regular file.")
        if info.st_nlink != 1:
            raise DesignerError("The saved routing state has multiple hard links.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DesignerError("Direct-API Designer is not configured; run setup first.") from None
    except DesignerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DesignerError("Could not read valid routing state.") from None
    try:
        state = routing_state.validate_routing_state(payload)
    except routing_state.RoutingStateError:
        raise DesignerError("The saved routing state is invalid.") from None
    try:
        belongs = Path(state["config_file"]).expanduser().resolve() == (root / "config.toml").resolve()
    except (OSError, RuntimeError):
        belongs = False
    if not belongs:
        raise DesignerError("The saved routing state belongs to another Codex home.")
    return state


def _load_authorized(home: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    root = home or codex_home()
    state = _read_routing_state(root)
    route = state.get("designer")
    if not isinstance(route, dict) or route.get("kind") != "designer-api":
        raise DesignerError("Direct Python API is not the configured Designer route.")
    try:
        config = load_config(codex_home_path=root)
    except DesignerApiConfigError as exc:
        raise DesignerError(str(exc)) from exc
    if not config["enabled"]:
        raise DesignerError("Direct-API Designer is disabled because provider api_key is empty.")
    provider = config["provider"]
    expected = {
        "provider": provider["id"],
        "model": provider["model"],
        "wire_api": provider["wire_api"],
        "endpoint_sha256": endpoint_sha256(config),
        "config_sha256": config_sha256(config),
    }
    if any(route.get(key) != value for key, value in expected.items()):
        raise DesignerError(
            "Designer API configuration changed after routing setup; run setup again before use."
        )
    return route, config


def check_config(home: Path | None = None) -> dict[str, Any]:
    route, config = _load_authorized(home)
    provider = config["provider"]
    return {
        "available": True,
        "role": "designer",
        "provider": provider["id"],
        "model": provider["model"],
        "api_url": provider["api_url"],
        "auth_type": provider["auth_type"],
        "wire_api": provider["wire_api"],
        "max_tokens": provider["max_tokens"],
        "transport": route["transport"],
        "api_source": route["api_source"],
        "path": route["path"],
        "model_call": False,
    }


def _validate_packet(packet: Any) -> str:
    if not isinstance(packet, str) or not packet.strip():
        raise DesignerError("`packet` must be a non-empty string for design handoff.")
    if len(packet) > MAX_INPUT_CHARS:
        raise DesignerError(f"design handoff input exceeds the {MAX_INPUT_CHARS}-character limit.")
    return packet


def create_design(packet: str) -> dict[str, Any]:
    checked = _validate_packet(packet)
    route, config = _load_authorized()
    provider = config["provider"]
    credential = (
        {"Authorization": f"Bearer {provider['api_key']}"}
        if provider["auth_type"] == "bearer"
        else {"x-api-key": provider["api_key"]}
    )
    body = {
        "model": provider["model"],
        "max_tokens": provider["max_tokens"],
        "system": DESIGNER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": checked}],
    }
    request = urllib_request.Request(
        provider["api_url"],
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "user-agent": "codex-orchestration-designer-api/0.9.1",
            **credential,
        },
        method="POST",
    )
    handlers: list[Any] = [NoRedirectHandler()]
    if urllib_parse.urlsplit(provider["api_url"]).hostname in LOCAL_HTTP_HOSTS:
        handlers.insert(0, urllib_request.ProxyHandler({}))
    opener = urllib_request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise DesignerError("Designer API returned an unsuccessful HTTP status.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DesignerError("Designer API response exceeded the bounded size limit.")
    except urllib_error.HTTPError as exc:
        status = exc.code if isinstance(exc.code, int) else "unknown"
        diagnostics = _safe_http_error_diagnostics(exc)
        exc.close()
        raise DesignerError(f"Designer API request failed with HTTP status {status}.{diagnostics}") from None
    except (TimeoutError, urllib_error.URLError, OSError):
        raise DesignerError("Designer API request failed due to a network or timeout error.") from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DesignerError("Designer API returned malformed JSON.") from None
    if not isinstance(payload, dict):
        raise DesignerError("Designer API returned an unexpected JSON value.")
    response_model = payload.get("model")
    if response_model != provider["model"]:
        raise DesignerError(
            "Strict model verification failed: Designer API requested model "
            f"{provider['model']!r} but the provider echoed {response_model!r}."
        )
    stop_reason = payload.get("stop_reason")
    if stop_reason == "refusal":
        details = payload.get("stop_details")
        details = details if isinstance(details, dict) else {}
        raise DesignerError(
            "Designer API response was refused; "
            f"refusal_type={_safe_field(details.get('type'))!r}; "
            f"category={_safe_field(details.get('category'))!r}."
        )
    if stop_reason != "end_turn":
        raise DesignerError(
            f"Designer API response did not complete with end_turn; stop_reason={stop_reason!r}."
        )
    content = payload.get("content")
    if not isinstance(content, list):
        raise DesignerError("Designer API returned an unexpected response.")
    blocks = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    ]
    lines = "\n".join(blocks).splitlines()
    signal_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if signal_index is None or lines[signal_index].strip() != "DESIGN_COMPLETE":
        raise DesignerError("Designer API omitted the required DESIGN_COMPLETE signal.")
    design = "\n".join(lines[signal_index + 1 :]).strip()
    if not design:
        raise DesignerError("Designer API returned no design handoff after DESIGN_COMPLETE.")
    return {
        "signal": "DESIGN_COMPLETE",
        "design": design,
        "provider": provider["id"],
        "model": provider["model"],
        "request_model": provider["model"],
        "response_model": response_model,
        "used_models": [response_model],
        "transport": route["transport"],
        "api_source": route["api_source"],
        "designer_path": route["path"],
    }


def status() -> dict[str, Any]:
    return check_config()


def tool_definitions() -> list[dict[str, Any]]:
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    return [
        {
            "name": "create_design",
            "title": "Create a bounded design handoff",
            "description": "Create one stateless design handoff with the configured API Designer.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "packet": {
                        "type": "string",
                        "maxLength": MAX_INPUT_CHARS,
                        "description": "Approved requirements, constraints, and requested design deliverables.",
                    }
                },
                "required": ["packet"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "status",
            "title": "Check direct-API Designer status",
            "description": "Validate the configured Designer route without a model call.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
            "serverInfo": {"name": "codex-orchestration-designer-api", "version": "1.0.0"},
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
            if not isinstance(arguments, dict):
                raise DesignerError("Tool arguments must be an object.")
            if name == "create_design":
                if set(arguments) != {"packet"}:
                    raise DesignerError("create_design requires only the packet argument.")
                result = _tool_result(create_design(arguments.get("packet")))
            elif name == "status":
                if arguments:
                    raise DesignerError("status accepts no arguments.")
                result = _tool_result(status())
            else:
                raise DesignerError(f"Unknown tool: {name!r}.")
        except DesignerError as exc:
            result = _tool_result(
                {"available": False, "error": str(exc), "recovery": STALE_BRIDGE_RECOVERY},
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
