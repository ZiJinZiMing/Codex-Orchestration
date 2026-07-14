from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
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
        (self.home / FABLE.STATE_FILENAME).write_text(
            json.dumps(
                {
                    "advisor": {
                        "kind": "fable",
                        "model": "claude-fable-5",
                        "effort": "max",
                        "server": "fable-advisor-python3",
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def write_route(self, profile: str | None = None) -> None:
        advisor: dict[str, object] = {
            "kind": "fable",
            "model": "claude-fable-5",
            "effort": "max",
            "server": "fable-advisor-python3",
        }
        if profile is not None:
            advisor["transport"] = {"kind": "claude-code", "profile": profile}
        (self.home / FABLE.STATE_FILENAME).write_text(
            json.dumps({"schema": 3, "advisor": advisor}), encoding="utf-8"
        )

    def write_cc_switch_fixture(self) -> tuple[Path, Path]:
        settings = self.home / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "model": "fable",
                    "env": {
                        "ANTHROPIC_BASE_URL": FABLE.CC_SWITCH_BASE_URL,
                        "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
                    },
                }
            ),
            encoding="utf-8",
        )
        database = self.home / "cc-switch.db"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                """
                CREATE TABLE providers (
                    id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_config TEXT NOT NULL,
                    is_current INTEGER NOT NULL
                );
                CREATE TABLE proxy_config (
                    app_type TEXT PRIMARY KEY,
                    proxy_enabled INTEGER NOT NULL,
                    listen_address TEXT NOT NULL,
                    listen_port INTEGER NOT NULL,
                    enabled INTEGER NOT NULL
                );
                CREATE TABLE proxy_request_logs (
                    request_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    app_type TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_model TEXT,
                    status_code INTEGER NOT NULL,
                    session_id TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO providers VALUES (?, ?, ?, ?, ?)",
                (
                    "openrouter-id",
                    "claude",
                    "OpenRouter",
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_DEFAULT_FABLE_MODEL": (
                                    "anthropic/claude-fable-5[1M]"
                                )
                            }
                        }
                    ),
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO proxy_config VALUES (?, ?, ?, ?, ?)",
                ("claude", 1, "127.0.0.1", 15721, 1),
            )
        return settings, database

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
                        "result": "No material gap found.",
                        "structured_output": {
                            "decision": "PLAN_APPROVED",
                            "review": "No material gap found.",
                        },
                        "modelUsage": {"claude-fable-5": {"outputTokens": 12}},
                    }
                ),
            )

        env = {
            "CODEX_HOME": str(self.home),
            "ANTHROPIC_API_KEY": "must-not-leak",
            "ANTHROPIC_AUTH_TOKEN": "must-not-leak",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }
        with (
            mock.patch.dict(os.environ, env, clear=False),
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
        self.assertEqual(review_command[review_command.index("--effort") + 1], "max")
        self.assertEqual(
            review_command[review_command.index("--prompt-suggestions") + 1], "false"
        )
        self.assertEqual(
            review_command[review_command.index("--setting-sources") + 1], "user"
        )
        schema = json.loads(
            review_command[review_command.index("--json-schema") + 1]
        )
        self.assertEqual(schema, FABLE.REVIEW_SCHEMA)
        self.assertEqual(review_kwargs["input"], "Review this complete plan.")
        self.assertEqual(calls[0][1]["encoding"], "utf-8")
        self.assertEqual(review_kwargs["encoding"], "utf-8")
        sanitized = review_kwargs["env"]
        self.assertIsInstance(sanitized, dict)
        for name in FABLE.SENSITIVE_ENV:
            self.assertNotIn(name, sanitized)

    def test_legacy_route_defaults_to_first_party_transport(self) -> None:
        route = FABLE.load_fable_route(self.home)
        self.assertEqual(route["transport_profile"], FABLE.FIRST_PARTY_PROFILE)

    def test_schema_three_requires_explicit_transport(self) -> None:
        self.write_route()
        with self.assertRaisesRegex(FABLE.AdvisorError, "transport is missing"):
            FABLE.load_fable_route(self.home)

    def test_cc_switch_review_requires_fresh_openrouter_fable_evidence(self) -> None:
        self.write_route(FABLE.CC_SWITCH_PROFILE)
        settings, database = self.write_cc_switch_fixture()
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[-2:] == ["auth", "status"]:
                return self.completed(
                    command,
                    json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "oauth_token",
                            "apiProvider": "firstParty",
                        }
                    ),
                )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "structured-output-helper",
                        "openrouter-id",
                        "claude",
                        "anthropic/claude-haiku-4.5",
                        "claude-haiku-4-5",
                        200,
                        command[command.index("--session-id") + 1],
                    ),
                )
                connection.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "fresh-request",
                        "openrouter-id",
                        "claude",
                        "anthropic/claude-5-fable-20260609",
                        "claude-fable-5",
                        200,
                        command[command.index("--session-id") + 1],
                    ),
                )
            return self.completed(
                command,
                json.dumps(
                    {
                        "result": "No material gap found.",
                        "structured_output": {
                            "decision": "PLAN_APPROVED",
                            "review": "No material gap found.",
                        },
                        "modelUsage": {"claude-fable-5": {"outputTokens": 12}},
                    }
                ),
            )

        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE, "claude_settings_path", return_value=settings),
            mock.patch.object(FABLE, "cc_switch_db_path", return_value=database),
            mock.patch.object(FABLE, "_check_cc_switch_health"),
            mock.patch.object(FABLE.subprocess, "run", side_effect=fake_run),
        ):
            result = FABLE.review_plan("Review this complete plan.")

        self.assertEqual(result["transport_profile"], FABLE.CC_SWITCH_PROFILE)
        self.assertEqual(result["route_confirmation"]["provider"], "OpenRouter")
        self.assertEqual(
            result["route_confirmation"]["model"],
            "anthropic/claude-5-fable-20260609",
        )
        self.assertEqual(result["route_confirmation"]["status_code"], 200)
        self.assertEqual(
            result["route_confirmation"]["request_model"], "claude-fable-5"
        )
        self.assertIn("--setting-sources", calls[1])
        self.assertIn("--session-id", calls[1])

    def test_cc_switch_review_fails_closed_without_unambiguous_new_evidence(self) -> None:
        self.write_route(FABLE.CC_SWITCH_PROFILE)
        settings, database = self.write_cc_switch_fixture()

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command[-2:] == ["auth", "status"]:
                return self.completed(
                    command,
                    json.dumps({"loggedIn": True, "authMethod": "oauth_token"}),
                )
            return self.completed(
                command,
                json.dumps(
                    {
                        "result": "Looks good.",
                        "structured_output": {
                            "decision": "PLAN_APPROVED",
                            "review": "Looks good.",
                        },
                        "modelUsage": {"claude-fable-5": {}},
                    }
                ),
            )

        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE, "claude_settings_path", return_value=settings),
            mock.patch.object(FABLE, "cc_switch_db_path", return_value=database),
            mock.patch.object(FABLE, "_check_cc_switch_health"),
            mock.patch.object(FABLE.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "fresh CC Switch"):
                FABLE.review_plan("packet")

    def test_cc_switch_preflight_rejects_wrong_mapping_without_exposing_secret(self) -> None:
        settings, database = self.write_cc_switch_fixture()
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE providers SET settings_config = ?",
                (json.dumps({"env": {"ANTHROPIC_DEFAULT_FABLE_MODEL": "other-model"}}),),
            )
        with (
            mock.patch.object(FABLE, "claude_settings_path", return_value=settings),
            mock.patch.object(FABLE, "cc_switch_db_path", return_value=database),
            mock.patch.object(FABLE, "_check_cc_switch_health"),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "Fable mapping") as raised:
                FABLE.verify_cc_switch_prerequisites()
        self.assertNotIn("fixture-secret", str(raised.exception))

    def test_cc_switch_confirmation_requires_the_exact_unique_session(self) -> None:
        _, database = self.write_cc_switch_fixture()
        with sqlite3.connect(database) as connection:
            for request_id, session_id in (
                ("unrelated", "other-session"),
                ("target-one", "target-session"),
                ("target-two", "target-session"),
            ):
                connection.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        "openrouter-id",
                        "claude",
                        "anthropic/claude-5-fable-20260609",
                        "anthropic/claude-fable-5",
                        200,
                        session_id,
                    ),
                )
        with mock.patch.object(FABLE, "cc_switch_db_path", return_value=database):
            with self.assertRaisesRegex(FABLE.AdvisorError, "fresh CC Switch"):
                FABLE.confirm_cc_switch_request(0, "missing-session")
            with self.assertRaisesRegex(FABLE.AdvisorError, "unambiguous"):
                FABLE.confirm_cc_switch_request(0, "target-session")

    def test_health_check_disables_proxies_and_rejects_redirected_urls(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"status":"healthy"}'
        response.geturl.return_value = FABLE.CC_SWITCH_HEALTH_URL
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with mock.patch.object(
            FABLE.urllib.request, "build_opener", return_value=opener
        ) as build:
            FABLE._check_cc_switch_health()
        handlers = build.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, FABLE.urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(isinstance(handler, FABLE._NoRedirectHandler) for handler in handlers))

        response.geturl.return_value = "http://example.invalid/health"
        with (
            mock.patch.object(FABLE.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(FABLE.AdvisorError, "health check failed"),
        ):
            FABLE._check_cc_switch_health()

    def test_review_failure_never_returns_raw_cli_diagnostics(self) -> None:
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
        failed = subprocess.CompletedProcess(
            ["claude"],
            1,
            "prompt and response body",
            "Authorization: Bearer sk-secret-value",
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, failed]),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "inspect Claude Code") as raised:
                FABLE.review_plan("packet")
        message = str(raised.exception)
        self.assertNotIn("sk-secret-value", message)
        self.assertNotIn("prompt and response body", message)

    def test_invalid_decision_or_unconfirmed_model_fails_closed(self) -> None:
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
                    "structured_output": {
                        "decision": "NOT_A_DECISION",
                        "review": "Looks good.",
                    },
                    "modelUsage": {"claude-fable-5": {}},
                }
            ),
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, malformed]),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "required plan decision"):
                FABLE.review_plan("packet")

        unconfirmed = self.completed(
            ["claude"],
            json.dumps(
                {
                    "result": "Fix the verification step.",
                    "structured_output": {
                        "decision": "PLAN_REVISE",
                        "review": "Fix the verification step.",
                    },
                    "modelUsage": {"claude-haiku-4-5": {}},
                }
            ),
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(FABLE.subprocess, "run", side_effect=[auth, unconfirmed]),
        ):
            with self.assertRaisesRegex(FABLE.AdvisorError, "did not confirm"):
                FABLE.review_plan("packet")

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
                    "transport_profile": FABLE.FIRST_PARTY_PROFILE,
                },
            ),
            mock.patch.object(
                FABLE,
                "check_claude_auth",
                return_value={"auth_method": "claude.ai", "api_provider": "firstParty"},
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
