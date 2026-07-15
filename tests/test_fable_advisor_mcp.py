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


class FakeHttpResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        )
        self.status = status

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.payload


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

    def set_direct_route(self, api_source: str = "user-settings") -> None:
        state_path = self.home / FABLE.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["advisor"].update(
            {
                "auth_mode": "api",
                "api_source": api_source,
                "transport": "direct-api",
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def write_user_api_settings(self, **env: str) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"env": env}), encoding="utf-8")

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

    def test_direct_api_sends_one_strict_messages_request_without_claude(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721/",
        )
        response = FakeHttpResponse(
            {
                "model": "anthropic/claude-fable-5",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "PLAN_APPROVED"},
                    {"type": "text", "text": "No material gap."},
                    {"type": "redacted_thinking", "data": "redacted"},
                ],
            }
        )
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE.urllib_request, "build_opener", return_value=opener),
            mock.patch.object(FABLE, "resolve_claude") as resolve_claude,
            mock.patch.object(FABLE.subprocess, "run") as run,
        ):
            result = FABLE.review_plan("complete packet")

        self.assertEqual(result["decision"], "PLAN_APPROVED")
        self.assertEqual(result["model"], "claude-fable-5")
        self.assertEqual(result["used_models"], ["claude-fable-5"])
        self.assertEqual(result["response_model"], "anthropic/claude-fable-5")
        self.assertEqual(result["model_echo_policy"], "exact-allowlist-v1")
        self.assertNotIn("private", result["review"])
        self.assertNotIn("redacted", result["review"])
        self.assertEqual(result["transport"], "direct-api")
        self.assertEqual(result["effort"], "not-applied")
        self.assertEqual(result["configured_effort"], "max")
        resolve_claude.assert_not_called()
        run.assert_not_called()
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:15721/v1/messages")
        self.assertEqual(
            opener.open.call_args.kwargs["timeout"], FABLE.DIRECT_API_TIMEOUT_SECONDS
        )
        self.assertEqual(FABLE.DIRECT_API_TIMEOUT_SECONDS, 600)
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer secret-token")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("x-api-key", headers)
        body = json.loads(request.data)
        self.assertEqual(
            body,
            {
                "model": "claude-fable-5",
                "max_tokens": 65536,
                "system": FABLE.SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": "complete packet"}],
            },
        )
        self.assertNotIn("effort", body)
        self.assertNotIn("output_config", body)

    def test_direct_api_key_and_route_validation_fail_closed(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_API_KEY="secret-key",
            ANTHROPIC_BASE_URL="https://gateway.example/anthropic",
        )
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.home)}, clear=True
        ):
            headers, endpoint, auth = FABLE.direct_api_configuration("user-settings")
        self.assertEqual(headers, {"x-api-key": "secret-key"})
        self.assertEqual(endpoint, "https://gateway.example/anthropic/v1/messages")
        self.assertEqual(auth["api_source"], "user-settings")

        invalid_urls = [
            "http://example.com",
            "http://127.0.0.2",
            "ftp://localhost",
            "https://user:pass@example.com",
            "https://example.com?token=secret",
            "https://example.com/#fragment",
        ]
        for url in invalid_urls:
            with self.subTest(url=url):
                with self.assertRaises(FABLE.AdvisorError):
                    FABLE._direct_api_endpoint(url)
        self.assertEqual(
            FABLE._direct_api_endpoint("http://localhost:8080"),
            "http://localhost:8080/v1/messages",
        )
        self.assertEqual(
            FABLE._direct_api_endpoint("http://[::1]:8080"),
            "http://[::1]:8080/v1/messages",
        )

    def test_direct_api_accepts_only_two_exact_model_echoes(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        for response_model in ("claude-fable-5", "anthropic/claude-fable-5"):
            with self.subTest(response_model=response_model):
                opener = mock.Mock()
                opener.open.return_value = FakeHttpResponse(
                    {
                        "model": response_model,
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                    }
                )
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    mock.patch.object(
                        FABLE.urllib_request, "build_opener", return_value=opener
                    ),
                ):
                    result = FABLE.review_plan("packet")
                self.assertEqual(result["model"], "claude-fable-5")
                self.assertEqual(result["used_models"], ["claude-fable-5"])
                self.assertEqual(result["response_model"], response_model)

    def test_direct_api_rejects_ambiguous_helper_and_custom_headers(self) -> None:
        self.set_direct_route()
        cases = [
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "secret-token",
                    "ANTHROPIC_API_KEY": "secret-key",
                },
                "pattern": "ambiguous",
            },
            {
                "env": {"ANTHROPIC_AUTH_TOKEN": "secret-token"},
                "apiKeyHelper": "helper-command",
                "pattern": "apiKeyHelper",
            },
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "secret-token",
                    "ANTHROPIC_CUSTOM_HEADERS": "X-Test: secret-header",
                },
                "pattern": "CUSTOM_HEADERS",
            },
        ]
        settings_path = self.home / ".claude" / "settings.json"
        settings_path.parent.mkdir(exist_ok=True)
        for case in cases:
            with self.subTest(pattern=case["pattern"]):
                payload = {"env": case["env"]}
                if "apiKeyHelper" in case:
                    payload["apiKeyHelper"] = case["apiKeyHelper"]
                settings_path.write_text(json.dumps(payload), encoding="utf-8")
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    self.assertRaisesRegex(FABLE.AdvisorError, case["pattern"]),
                ):
                    FABLE.direct_api_configuration("user-settings")

    def test_direct_api_response_contract_and_errors_never_fallback(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        invalid_payloads = [
            ({"stop_reason": "end_turn", "content": []}, "unapproved model echo"),
            (
                {"model": 5, "stop_reason": "end_turn", "content": []},
                "unapproved model echo",
            ),
            (
                {
                    "model": "claude-fable-5-20260715",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "unapproved model echo",
            ),
            (
                {
                    "model": "openrouter/claude-fable-5",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "unapproved model echo",
            ),
            (
                {
                    "model": "Anthropic/claude-fable-5",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "unapproved model echo",
            ),
            (
                {
                    "model": " anthropic/claude-fable-5",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "unapproved model echo",
            ),
            (
                {
                    "model": "anthropic/claude-fable-5 ",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "unapproved model echo",
            ),
            (
                {
                    "model": "claude-fable-5",
                    "stop_reason": "max_tokens",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "end_turn",
            ),
            (
                {
                    "model": "claude-fable-5",
                    "stop_reason": "tool_use",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "end_turn",
            ),
            (
                {
                    "model": "claude-fable-5",
                    "stop_reason": "pause_turn",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "end_turn",
            ),
            (
                {
                    "model": "claude-fable-5",
                    "content": [{"type": "text", "text": "PLAN_APPROVED"}],
                },
                "end_turn",
            ),
            (
                {
                    "model": "claude-fable-5",
                    "stop_reason": "end_turn",
                    "content": [{"type": "thinking", "thinking": "only"}],
                },
                "no review text",
            ),
        ]
        for payload, pattern in invalid_payloads:
            with self.subTest(pattern=pattern, payload=payload):
                opener = mock.Mock()
                opener.open.return_value = FakeHttpResponse(payload)
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    mock.patch.object(
                        FABLE.urllib_request, "build_opener", return_value=opener
                    ),
                    mock.patch.object(FABLE, "resolve_claude") as resolve_claude,
                    mock.patch.object(FABLE.subprocess, "run") as run,
                    self.assertRaisesRegex(FABLE.AdvisorError, pattern),
                ):
                    FABLE.review_plan("packet")
                opener.open.assert_called_once()
                resolve_claude.assert_not_called()
                run.assert_not_called()

    def test_direct_api_redirect_and_network_errors_are_secret_safe(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        for status in (301, 308, 305, 401, 429, 500):
            with self.subTest(status=status):
                opener = mock.Mock()
                opener.open.side_effect = FABLE.urllib_error.HTTPError(
                    "https://secret-token@example.invalid/path",
                    status,
                    "redirect secret-token",
                    {"Location": "https://evil.invalid/secret-token"},
                    None,
                )
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    mock.patch.object(
                        FABLE.urllib_request, "build_opener", return_value=opener
                    ),
                    self.assertRaises(FABLE.AdvisorError) as caught,
                ):
                    FABLE.review_plan("packet")
                message = str(caught.exception)
                self.assertIn(str(status), message)
                self.assertNotIn("secret-token", message)
                self.assertNotIn("evil.invalid", message)
                self.assertNotIn("Location", message)
                opener.open.assert_called_once()

        for code in (301, 302, 303, 305, 307, 308):
            with self.subTest(handler_code=code):
                self.assertIsNone(
                    FABLE.NoRedirectHandler().redirect_request(
                        mock.Mock(), None, code, "redirect", {}, "https://evil.invalid"
                    )
                )

        for failure in (
            TimeoutError("secret-token"),
            FABLE.urllib_error.URLError("secret-token"),
            OSError("secret-token"),
        ):
            with self.subTest(failure=type(failure).__name__):
                opener = mock.Mock()
                opener.open.side_effect = failure
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    mock.patch.object(
                        FABLE.urllib_request, "build_opener", return_value=opener
                    ),
                    self.assertRaises(FABLE.AdvisorError) as caught,
                ):
                    FABLE.review_plan("packet")
                self.assertNotIn("secret-token", str(caught.exception))
                opener.open.assert_called_once()

    def test_direct_api_malformed_payloads_and_status_fail_closed(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        for payload, pattern in (
            (b"not-json", "malformed JSON"),
            (["not", "an", "object"], "unexpected JSON value"),
        ):
            with self.subTest(pattern=pattern):
                opener = mock.Mock()
                opener.open.return_value = FakeHttpResponse(payload)
                with (
                    mock.patch.dict(
                        os.environ, {"CODEX_HOME": str(self.home)}, clear=True
                    ),
                    mock.patch.object(
                        FABLE.urllib_request, "build_opener", return_value=opener
                    ),
                    self.assertRaisesRegex(FABLE.AdvisorError, pattern),
                ):
                    FABLE.review_plan("packet")

        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.home)}, clear=True
        ):
            status = FABLE.advisor_status(FABLE.load_fable_route())
        self.assertTrue(status["available"])
        self.assertEqual(status["transport"], "direct-api")
        self.assertEqual(status["effort"], "not-applied")
        self.assertEqual(status["configured_effort"], "max")
        self.assertNotIn("secret-token", json.dumps(status))

    def test_legacy_route_defaults_to_claude_code_transport(self) -> None:
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.home)}, clear=True
        ):
            route = FABLE.load_fable_route()
        self.assertEqual(route["transport"], "claude-code")

        state_path = self.home / FABLE.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["advisor"]["transport"] = "direct-api"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            self.assertRaisesRegex(FABLE.AdvisorError, "requires Claude api"),
        ):
            FABLE.load_fable_route()

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
