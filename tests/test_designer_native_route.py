from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-orchestration" / "skills" / "codex-orchestration" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure_designer_api as CONFIG  # noqa: E402
import configure_native_routing as NATIVE  # noqa: E402
import routing_state as STATE  # noqa: E402


def designer_route() -> dict[str, str]:
    return {
        "kind": "designer-api",
        "provider": "kimi",
        "model": "k3",
        "wire_api": "anthropic-messages",
        "endpoint_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "server": "designer-api-python",
        "transport": STATE.DESIGNER_API_TRANSPORT,
        "api_source": STATE.DESIGNER_API_SOURCE,
        "path": STATE.DESIGNER_API_PATH,
    }


class DesignerNativeRouteTests(unittest.TestCase):
    def parse(self, *args: str):
        with mock.patch.object(sys, "argv", ["configure_native_routing.py", *args]):
            return NATIVE.parse_args()

    def test_designer_api_and_effort_contract(self) -> None:
        omitted = self.parse("--executor-model", "gpt-5.6-luna", "--designer-api")
        NATIVE._validate_args(omitted)
        self.assertIsNone(omitted.designer_effort)
        for effort in ("auto", "high"):
            args = self.parse(
                "--executor-model",
                "gpt-5.6-luna",
                "--designer-api",
                "--designer-effort",
                effort,
            )
            with self.assertRaisesRegex(NATIVE.ConfigurationError, "not applicable"):
                NATIVE._validate_args(args)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(
                "--executor-model",
                "gpt-5.6-luna",
                "--designer-api",
                "--designer-model",
                "gpt-5.6-sol",
            )

    def test_policy_calls_designer_mcp_instead_of_spawn(self) -> None:
        route = designer_route()
        _, usage = NATIVE.build_policy(
            {"kind": "model", "model": "gpt-5.6-luna", "effort": "xhigh"},
            None,
            None,
            route,
        )
        self.assertIn("`create_design`", usage)
        self.assertIn('"designer-api-python"', usage)
        self.assertIn("Require DESIGN_COMPLETE", usage)
        self.assertNotIn('model = "k3"', usage)

    def test_setup_state_enables_independent_launcher_families(self) -> None:
        executor = {"kind": "model", "model": "gpt-5.6-luna", "effort": "xhigh"}
        advisor = {
            "kind": "fable",
            "model": STATE.FABLE_MODEL,
            "effort": "high",
            "server": "fable-advisor-python",
        }
        state, _, _ = NATIVE._prepare_setup_state(
            {"features": {"multi_agent_v2": {}}},
            None,
            f"{STATE.MANAGED_MARKER}\nmode",
            f"{STATE.MANAGED_MARKER}\nusage",
            executor,
            None,
            advisor,
            designer_route(),
            Path("C:/tmp/codex/config.toml"),
            False,
        )
        self.assertEqual(state["schema"], 5)
        self.assertEqual(
            {server for server, enabled in state["managed"]["mcp"].items() if enabled},
            {"fable-advisor-python", "designer-api-python"},
        )
        STATE.validate_routing_state(state)

    def test_schema_four_is_read_only_until_explicit_setup_upgrades_to_five(self) -> None:
        executor = {"kind": "model", "model": "gpt-5.6-luna", "effort": "xhigh"}
        designer = {"kind": "model", "model": "gpt-5.6-sol", "effort": "high"}
        old_mode = f"{STATE.MANAGED_MARKER}\nold mode"
        old_usage = f"{STATE.MANAGED_MARKER}\nold usage"
        legacy, _, _ = NATIVE._prepare_setup_state(
            {"features": {"multi_agent_v2": {}}},
            None,
            old_mode,
            old_usage,
            executor,
            None,
            None,
            designer,
            Path("C:/tmp/codex/config.toml"),
            False,
        )
        legacy["schema"] = 4
        legacy["policy_version"] = 4
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / NATIVE.STATE_FILENAME
            original = json.dumps(legacy, indent=2) + "\n"
            state_path.write_text(original, encoding="utf-8")
            loaded = NATIVE._read_state(state_path)
            self.assertEqual(state_path.read_text(encoding="utf-8"), original)

        current = {
            "features": {
                "multi_agent_v2": {
                    "hide_spawn_agent_metadata": False,
                    "tool_namespace": "agents",
                    "multi_agent_mode_hint_text": old_mode,
                    "usage_hint_text": old_usage,
                }
            }
        }
        upgraded, _, _ = NATIVE._prepare_setup_state(
            current,
            loaded,
            f"{STATE.MANAGED_MARKER}\nnew mode",
            f"{STATE.MANAGED_MARKER}\nnew usage",
            executor,
            None,
            None,
            designer_route(),
            Path("C:/tmp/codex/config.toml"),
            False,
        )
        self.assertEqual(upgraded["schema"], 5)
        self.assertEqual(upgraded["policy_version"], 5)
        for key, value in legacy["previous"].items():
            self.assertEqual(upgraded["previous"][key], value)
        self.assertEqual(
            upgraded["previous"]["mcp"]["designer-api-python"],
            {"known": True, "present": False},
        )
        STATE.validate_routing_state(upgraded)

    def test_designer_config_preflight_is_nonsecret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            value = {
                "schema": 1,
                "role": "designer",
                "provider": {
                    "id": "kimi",
                    "api_url": "https://api.kimi.com/coding/v1/messages",
                    "api_key": "must-not-return",
                    "model": "k3",
                    "auth_type": "bearer",
                    "wire_api": "anthropic-messages",
                    "max_tokens": 16384,
                },
            }
            CONFIG.config_path(home).write_text(json.dumps(value), encoding="utf-8")
            details = NATIVE.verify_designer_api_prerequisites(home)
            self.assertEqual(details["provider"], "kimi")
            self.assertEqual(details["model"], "k3")
            self.assertNotIn("api_key", details)


if __name__ == "__main__":
    unittest.main()
