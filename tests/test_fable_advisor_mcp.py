from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "codex-orchestration"
    / "skills"
    / "codex-orchestration"
    / "scripts"
    / "fable_advisor_mcp.py"
)
SPEC = importlib.util.spec_from_file_location("fable_advisor_mcp", SCRIPT)
assert SPEC and SPEC.loader
FABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FABLE)


class FableAdvisorMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.home_patch = mock.patch.object(FABLE.Path, "home", return_value=self.home)
        self.home_patch.start()
        (self.home / FABLE.STATE_FILENAME).write_text(
            json.dumps(
                {
                    "advisor": {
                        "kind": "fable",
                        "model": "claude-fable-5",
                        "effort": "max",
                        "server": "fable-advisor-python3",
                        "auth_mode": "subscription",
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def test_review_is_pinned_sanitized_read_only_and_runtime_confirmed(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            if command[-2:] == ["auth", "status"]:
                return self.completed(
                    command,
                    json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "claude.ai",
                            "apiProvider": "firstParty",
                            "subscriptionType": "max",
                        }
                    ),
                )
            return self.completed(
                command,
                json.dumps(
                    {
                        "result": "PLAN_APPROVED\nNo material gap found.",
                        "modelUsage": {"claude-fable-5": {"outputTokens": 12}},
                    }
                ),
            )

        env = {
            "CODEX_HOME": str(self.home),
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=fake_run),
        ):
            result = FABLE.review_plan("Review this complete plan.")

        self.assertEqual(result["decision"], "PLAN_APPROVED")
        self.assertEqual(result["model"], "claude-fable-5")
        self.assertEqual(result["used_models"], ["claude-fable-5"])
        self.assertNotIn("subscription_type", result)
        review_command, review_kwargs = calls[1]
        self.assertIn("--safe-mode", review_command)
        self.assertNotIn("--bare", review_command)
        self.assertEqual(review_command[review_command.index("--model") + 1], "claude-fable-5")
        self.assertEqual(
            review_command[review_command.index("--name") + 1],
            "codex-fable-review",
        )
        self.assertNotIn("--fallback-model", review_command)
        self.assertEqual(review_command[review_command.index("--effort") + 1], "max")
        self.assertEqual(
            review_command[review_command.index("--prompt-suggestions") + 1], "false"
        )
        self.assertEqual(review_kwargs["input"], "Review this complete plan.")
        sanitized = review_kwargs["env"]
        self.assertIsInstance(sanitized, dict)
        for name in FABLE.SENSITIVE_ENV:
            self.assertNotIn(name, sanitized)
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"], "1")
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"], "1")
        for name in FABLE.CLAUDE_MODEL_ENV:
            self.assertEqual(sanitized[name], "claude-fable-5")

    def test_missing_decision_is_conservatively_revised_and_model_is_confirmed(self) -> None:
        auth = self.completed(
            ["claude", "auth", "status"],
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "pro",
                }
            ),
        )
        malformed = self.completed(
            ["claude"],
            json.dumps(
                {
                    "result": "Looks good.",
                    "modelUsage": {"claude-fable-5": {}},
                }
            ),
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, malformed]),
        ):
            result = FABLE.review_plan("packet")
        self.assertEqual(result["decision"], "PLAN_REVISE")
        self.assertTrue(result["review"].startswith("PLAN_REVISE\n\nLooks good."))

        unconfirmed = self.completed(
            ["claude"],
            json.dumps(
                {
                    "result": "PLAN_REVISE\nFix the verification step.",
                    "modelUsage": {"claude-haiku-4-5": {}},
                }
            ),
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, unconfirmed]),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "expected only"):
                FABLE.review_plan("packet")

    def test_api_user_settings_supports_event_output_without_subscription(self) -> None:
        route = json.loads(
            (self.home / FABLE.STATE_FILENAME).read_text(encoding="utf-8")
        )
        route["advisor"].update(
            {"auth_mode": "api", "api_source": "user-settings"}
        )
        (self.home / FABLE.STATE_FILENAME).write_text(
            json.dumps(route), encoding="utf-8"
        )
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "configured"}}),
            encoding="utf-8",
        )
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Useful review without marker."}]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "modelUsage": {"claude-fable-5": {}},
            },
        ]
        completed = self.completed(["claude"], json.dumps(events))
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", return_value=completed) as run,
        ):
            result = FABLE.review_plan("packet")

        self.assertEqual(result["decision"], "PLAN_REVISE")
        self.assertEqual(result["auth_path"], "api")
        self.assertEqual(result["api_source"], "user-settings")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertIn("--bare", command)
        self.assertEqual(
            run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], "configured"
        )
        self.assertEqual(
            run.call_args.kwargs["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"],
            "claude-fable-5",
        )

    def test_fable_failure_has_no_model_fallback(self) -> None:
        auth = self.completed(
            ["claude", "auth", "status"],
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            ),
        )
        failed = subprocess.CompletedProcess(["claude"], 1, "", "unavailable")
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(
                FABLE.subprocess, "run", side_effect=[auth, failed]
            ) as run,
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "exited with 1"):
                FABLE.review_plan("packet")

        self.assertEqual(run.call_count, 2)
        review_command = run.call_args_list[1].args[0]
        self.assertNotIn("--fallback-model", review_command)

    def test_mixed_or_missing_model_metadata_fails_closed(self) -> None:
        auth = self.completed(
            ["claude", "auth", "status"],
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            ),
        )
        mixed = self.completed(
            ["claude"],
            json.dumps(
                {
                    "result": "PLAN_APPROVED\nNo material gap found.",
                    "modelUsage": {
                        "claude-fable-5": {},
                        "claude-haiku-4-5": {},
                    },
                }
            ),
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, mixed]),
        ):
            response = FABLE.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "review_plan",
                        "arguments": {"packet": "packet"},
                    },
                }
            )
        tool_result = response["result"]
        self.assertTrue(tool_result["isError"])
        self.assertNotIn("PLAN_APPROVED", tool_result["content"][0]["text"])
        self.assertIn("expected only claude-fable-5", tool_result["content"][0]["text"])

        missing = self.completed(
            ["claude"], json.dumps({"result": "PLAN_APPROVED\nLooks good."})
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, missing]),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "omitted modelUsage"):
                FABLE.review_plan("packet")

    def test_subscription_rejects_api_configuration_before_model_call(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(self.home), "ANTHROPIC_API_KEY": "configured"},
                clear=True,
            ),
            mock.patch.object(FABLE.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "API/Gateway"):
                FABLE.check_claude_auth(Path("/fake/claude"), "subscription")
        run.assert_not_called()

    def test_mcp_surface_exposes_only_bounded_tools(self) -> None:
        initialized = FABLE.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "codex-orchestration-fable-advisor")
        listed = FABLE.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = listed["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["review_plan", "status"])
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
        review_schema = tools[0]["inputSchema"]
        self.assertEqual(review_schema["required"], ["packet"])
        self.assertFalse(review_schema["additionalProperties"])

    def test_status_does_not_expose_account_plan_metadata(self) -> None:
        with (
            mock.patch.object(
                FABLE,
                "load_fable_route",
                return_value={
                    "model": "claude-fable-5",
                    "effort": "max",
                    "auth_mode": "api",
                    "api_source": "user-settings",
                },
            ),
            mock.patch.object(
                FABLE,
                "check_claude_auth",
                return_value={
                    "auth_mode": "api",
                    "auth_path": "api",
                    "auth_method": "api",
                    "api_source": "user-settings",
                },
            ),
        ):
            response = FABLE.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "status", "arguments": {}},
                }
            )
        text = response["result"]["content"][0]["text"]
        self.assertNotIn("subscription", text.lower())
        self.assertNotIn("account_plan", text)


if __name__ == "__main__":
    unittest.main()
