from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib import error as urllib_error


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-orchestration" / "skills" / "codex-orchestration" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure_designer_api as CONFIG  # noqa: E402
import designer_api_mcp as MCP  # noqa: E402
import routing_state as STATE  # noqa: E402


def snapshot(value: object = None, *, present: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"known": True, "present": present}
    if present:
        result["value"] = value
    return result


def provider_config(api_key: str = "secret-token") -> dict[str, object]:
    return {
        "schema": 1,
        "role": "designer",
        "provider": {
            "id": "kimi",
            "api_url": "https://api.kimi.com/coding/v1/messages",
            "api_key": api_key,
            "model": "k3",
            "auth_type": "bearer",
            "wire_api": "anthropic-messages",
            "max_tokens": 16384,
        },
    }


def route(config: dict[str, object]) -> dict[str, str]:
    checked = CONFIG.validate_config(config)
    provider = checked["provider"]
    return {
        "kind": "designer-api",
        "provider": provider["id"],
        "model": provider["model"],
        "wire_api": provider["wire_api"],
        "endpoint_sha256": CONFIG.endpoint_sha256(checked),
        "config_sha256": CONFIG.config_sha256(checked),
        "server": "designer-api-python",
        "transport": STATE.DESIGNER_API_TRANSPORT,
        "api_source": STATE.DESIGNER_API_SOURCE,
        "path": STATE.DESIGNER_API_PATH,
    }


def state(home: Path, selected: dict[str, str]) -> dict[str, object]:
    return {
        "schema": 5,
        "policy_version": 5,
        "managed_by": "codex-orchestration",
        "config_file": str(home / "config.toml"),
        "executor": {"kind": "model", "model": "gpt-5.6-luna", "effort": "xhigh"},
        "planner": None,
        "advisor": None,
        "designer": selected,
        "managed": {
            "mode": f"{STATE.MANAGED_MARKER}\nmode",
            "usage": f"{STATE.MANAGED_MARKER}\nusage",
            "metadata": False,
            "namespace": STATE.ROUTING_TOOL_NAMESPACE,
            "mcp": {"designer-api-python": True},
        },
        "previous": {
            "mode": snapshot(),
            "usage": snapshot(),
            "metadata": snapshot(),
            "namespace": snapshot(),
            "mcp": {"designer-api-python": snapshot()},
        },
        "scalar_origin": None,
        "managed_feature": None,
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, limit: int) -> bytes:
        return self.payload


class DesignerApiMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = provider_config()
        CONFIG.config_path(self.home).write_text(json.dumps(self.config), encoding="utf-8")
        (self.home / MCP.STATE_FILENAME).write_text(
            json.dumps(state(self.home, route(self.config))), encoding="utf-8"
        )
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=False)
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def success_payload(self, text: str = "DESIGN_COMPLETE\nUse a two-column layout.") -> dict[str, object]:
        return {
            "model": "k3",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        }

    def test_status_is_nonsecret_and_makes_no_model_call(self) -> None:
        with mock.patch.object(MCP.urllib_request, "build_opener") as build:
            result = MCP.status()
        build.assert_not_called()
        self.assertFalse(result["model_call"])
        self.assertEqual(result["provider"], "kimi")
        self.assertNotIn("api_key", result)

    def test_create_design_sends_exact_bounded_request(self) -> None:
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(
            self.success_payload("\nDESIGN_COMPLETE\n- Layout\n  - Preserve nesting")
        )
        with mock.patch.object(MCP.urllib_request, "build_opener", return_value=opener):
            result = MCP.create_design("Design a settings page.")
        self.assertEqual(result["signal"], "DESIGN_COMPLETE")
        self.assertEqual(result["response_model"], "k3")
        self.assertEqual(result["design"], "- Layout\n  - Preserve nesting")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "k3")
        self.assertEqual(body["max_tokens"], 16384)
        self.assertEqual(body["messages"], [{"role": "user", "content": "Design a settings page."}])
        self.assertIn("DESIGN_COMPLETE", body["system"])
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Anthropic-version"), MCP.ANTHROPIC_VERSION)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], MCP.HTTP_TIMEOUT_SECONDS)

    def test_route_binding_detects_nonsecret_drift_but_allows_key_rotation(self) -> None:
        changed = provider_config("rotated-token")
        CONFIG.config_path(self.home).write_text(json.dumps(changed), encoding="utf-8")
        self.assertTrue(MCP.status()["available"])
        changed["provider"]["model"] = "other"
        CONFIG.config_path(self.home).write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(MCP.DesignerError, "changed after routing setup"):
            MCP.status()

    def test_signal_failures_return_no_partial_design(self) -> None:
        for text in (
            "Use a grid.",
            "Prelude\nDESIGN_COMPLETE\nUse a grid.",
            "DESIGN_COMPLETE",
        ):
            with self.subTest(text=text):
                opener = mock.Mock()
                opener.open.return_value = FakeResponse(self.success_payload(text))
                with mock.patch.object(MCP.urllib_request, "build_opener", return_value=opener):
                    with self.assertRaises(MCP.DesignerError):
                        MCP.create_design("packet")

    def test_response_validation_fails_closed(self) -> None:
        cases = [
            {"model": "wrong", "stop_reason": "end_turn", "content": [{"type": "text", "text": "DESIGN_COMPLETE\nbody"}]},
            {"model": "k3", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "DESIGN_COMPLETE\nbody"}]},
            {"model": "k3", "stop_reason": "end_turn", "content": []},
            {"model": "k3", "stop_reason": "refusal", "stop_details": {"type": "policy"}, "content": []},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                opener = mock.Mock()
                opener.open.return_value = FakeResponse(payload)
                with mock.patch.object(MCP.urllib_request, "build_opener", return_value=opener):
                    with self.assertRaises(MCP.DesignerError):
                        MCP.create_design("packet")

    def test_redirect_http_and_network_fail_once_without_secret_leak(self) -> None:
        failures = [
            urllib_error.HTTPError(
                "https://api.kimi.com/coding/v1/messages",
                302,
                "redirect secret-token",
                {},
                io.BytesIO(b'{"error":{"type":"redirect"}}'),
            ),
            urllib_error.URLError("network secret-token"),
        ]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with mock.patch.object(MCP.urllib_request, "build_opener", return_value=opener):
                    with self.assertRaises(MCP.DesignerError) as caught:
                        MCP.create_design("packet")
                opener.open.assert_called_once()
                self.assertNotIn("secret-token", str(caught.exception))

    def test_tool_surface_is_exact(self) -> None:
        tools = MCP.tool_definitions()
        self.assertEqual([tool["name"] for tool in tools], ["create_design", "status"])
        self.assertFalse(tools[0]["inputSchema"]["additionalProperties"])
        response = MCP.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "status", "arguments": {"extra": True}}}
        )
        self.assertTrue(response["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
