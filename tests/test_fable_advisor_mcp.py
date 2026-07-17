from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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
sys.path.insert(0, str(SCRIPT.parent))
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
        self.write_state(advisor=self.route("high"))

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def route(effort: str = "high") -> dict[str, str]:
        return {
            "kind": "fable",
            "model": "claude-fable-5",
            "effort": effort,
            "server": "fable-advisor-python3",
        }

    def write_state(
        self, *, schema: int = 4, policy_version: int | None = None, **seats: object
    ) -> None:
        effective_policy = schema if policy_version is None else policy_version
        advisor = seats.get("advisor")
        if (
            (schema, effective_policy) == (4, 4)
            and isinstance(advisor, dict)
            and advisor.get("kind") == "fable"
            and "auth_mode" not in advisor
        ):
            seats["advisor"] = {
                **advisor,
                "auth_mode": "auto",
                "transport": "claude-code",
                "path": "claude-code-cli",
            }
        fable_routes = [
            route
            for route in seats.values()
            if isinstance(route, dict) and route.get("kind") == "fable"
        ]
        managed_mcp = {
            route["server"]: True
            for route in fable_routes[:1]
            if isinstance(route.get("server"), str)
        }
        previous_mcp = {
            server: {"known": True, "present": False}
            for server in managed_mcp
        }
        payload = {
            "schema": schema,
            "policy_version": effective_policy,
            "managed_by": "codex-orchestration",
            "config_file": str(self.home / "config.toml"),
            "executor": {
                "kind": "model",
                "model": "gpt-5.6-luna",
                "effort": "xhigh",
            },
            "advisor": None,
            "managed": {
                "mode": f"{FABLE.MANAGED_MARKER}\nmode",
                "usage": f"{FABLE.MANAGED_MARKER}\nusage",
                "metadata": False,
                "namespace": "agents",
                "mcp": managed_mcp,
            },
            "previous": {
                "mode": {"known": True, "present": False},
                "usage": {"known": True, "present": False},
                "metadata": {"known": True, "present": False},
                "namespace": {"known": True, "present": False},
                "mcp": previous_mcp,
            },
            "scalar_origin": None,
            "managed_feature": None,
            **seats,
        }
        if (schema, effective_policy) in {(3, 3), (4, 4)} and "planner" not in payload:
            payload["planner"] = None
        if (schema, effective_policy) in {(3, 2), (4, 4)}:
            payload["managed"]["enabled"] = True
            payload["previous"]["enabled"] = {
                "known": True,
                "present": False,
            }
        (self.home / FABLE.STATE_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    @staticmethod
    def completed(
        command: list[str], stdout: str, *, returncode: int = 0, stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def auth_result(self) -> subprocess.CompletedProcess[str]:
        return self.completed(
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

    def model_result(
        self, response: str, *, model_usage: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.completed(
            ["claude"],
            json.dumps(
                {
                    "result": response,
                    "modelUsage": model_usage
                    if model_usage is not None
                    else {"claude-fable-5": {"outputTokens": 12}},
                }
            ),
        )

    def invoke_with_results(
        self,
        function: object,
        *args: str,
        model_response: str,
        model_usage: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], list[tuple[list[str], dict[str, object]]]]:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            if command[-2:] == ["auth", "status"]:
                return self.auth_result()
            return self.model_result(model_response, model_usage=model_usage)

        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(
                FABLE, "resolve_claude", return_value=Path("/fake/claude")
            ),
            mock.patch.object(FABLE.subprocess, "run", side_effect=fake_run),
        ):
            result = function(*args)
        return result, calls

    def set_direct_route(self, api_source: str = "user-settings") -> None:
        state_path = self.home / FABLE.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["advisor"].update(
            {
                "auth_mode": "api",
                "api_source": api_source,
                "transport": "direct-api",
                "path": "ccswitch"
                if api_source == "user-settings"
                else "python-api",
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def write_user_api_settings(self, **env: str) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"env": env}), encoding="utf-8")

    def write_standalone_api_config(
        self,
        *,
        credential: str = "standalone-secret",
        api_url: str = "https://openrouter.ai/api/v1/messages",
        model: str = "anthropic/claude-fable-5",
        auth_type: str = "bearer",
    ) -> None:
        (self.home / ".codex-orchestration-fable-api.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "api_url": api_url,
                    "model": model,
                    "auth_type": auth_type,
                    "credential": credential,
                }
            ),
            encoding="utf-8",
        )

    def write_python_api_provider(
        self,
        *,
        api_key: str = "",
        api_url: str = "https://openrouter.ai/api/v1/messages",
        model: str = "anthropic/claude-fable-5",
        auth_type: str = "bearer",
    ) -> None:
        (self.home / ".codex-orchestration-fable-api.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "provider": {
                        "api_url": api_url,
                        "api_key": api_key,
                        "model": model,
                        "auth_type": auth_type,
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_review_is_pinned_sanitized_read_only_and_runtime_confirmed(self) -> None:
        env = {
            "CODEX_HOME": str(self.home),
        }
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            if command[-2:] == ["auth", "status"]:
                return self.auth_result()
            return self.model_result("PLAN_APPROVED\nNo material gap found.")

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
        auth_command, auth_kwargs = calls[0]
        self.assertEqual(auth_command[-2:], ["auth", "status"])
        review_command, review_kwargs = calls[1]
        for flag in (
            "--safe-mode",
            "--tools",
            "--permission-mode",
            "--no-session-persistence",
            "--prompt-suggestions",
            "--output-format",
            "--system-prompt",
        ):
            self.assertIn(flag, review_command)
        self.assertNotIn("--bare", review_command)
        self.assertEqual(review_command[review_command.index("--tools") + 1], "")
        self.assertEqual(review_command[review_command.index("--model") + 1], "claude-fable-5")
        self.assertEqual(
            review_command[review_command.index("--name") + 1],
            "codex-fable-review",
        )
        self.assertNotIn("--fallback-model", review_command)
        self.assertEqual(
            review_command[review_command.index("--permission-mode") + 1], "dontAsk"
        )
        self.assertEqual(
            review_command[review_command.index("--model") + 1], "claude-fable-5"
        )
        self.assertEqual(review_command[review_command.index("--effort") + 1], "high")
        self.assertEqual(
            review_command[review_command.index("--prompt-suggestions") + 1],
            "false",
        )
        self.assertEqual(
            review_command[review_command.index("--output-format") + 1], "json"
        )
        self.assertEqual(review_kwargs["input"], "Review this complete plan.")
        for kwargs in (auth_kwargs, review_kwargs):
            sanitized = kwargs["env"]
            self.assertIsInstance(sanitized, dict)
            for name in FABLE.SENSITIVE_ENV:
                self.assertNotIn(name, sanitized)
        sanitized = review_kwargs["env"]
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"], "1")
        self.assertEqual(sanitized["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"], "1")
        for name in FABLE.CLAUDE_MODEL_ENV:
            self.assertEqual(sanitized[name], "claude-fable-5")

    def test_runtime_model_policy_accepts_only_fable_and_exact_allowed_helper(
        self,
    ) -> None:
        allowed_scenarios = (
            ({FABLE.FABLE_MODEL: {"outputTokens": 12}}, [FABLE.FABLE_MODEL]),
            (
                {
                    FABLE.FABLE_MODEL: {"outputTokens": 12},
                    FABLE.FABLE_HELPER_MODEL: {"outputTokens": 1},
                },
                sorted((FABLE.FABLE_MODEL, FABLE.FABLE_HELPER_MODEL)),
            ),
        )
        for model_usage, expected_models in allowed_scenarios:
            with self.subTest(model_usage=model_usage):
                result, _ = self.invoke_with_results(
                    FABLE.review_plan,
                    "packet",
                    model_response="PLAN_APPROVED\nNo material gap found.",
                    model_usage=model_usage,
                )
                self.assertEqual(result["decision"], "PLAN_APPROVED")
                self.assertEqual(result["model"], FABLE.FABLE_MODEL)
                self.assertEqual(result["used_models"], expected_models)

        secret = "TOP-SECRET-MODEL-OUTPUT"
        rejected_scenarios = (
            (
                {
                    FABLE.FABLE_MODEL: {"outputTokens": 12},
                    "claude-haiku-4-5-20251002": {"outputTokens": 1},
                },
                "outside the allowed Fable runtime policy",
            ),
            (
                {FABLE.FABLE_HELPER_MODEL: {"outputTokens": 1}},
                "did not confirm the pinned Claude Fable 5 primary model",
            ),
        )
        for model_usage, expected_error in rejected_scenarios:
            with self.subTest(model_usage=model_usage):
                with self.assertRaisesRegex(
                    FABLE.AdvisorError, expected_error
                ) as failure:
                    self.invoke_with_results(
                        FABLE.review_plan,
                        "packet",
                        model_response=f"PLAN_APPROVED\n{secret}",
                        model_usage=model_usage,
                    )
                self.assertNotIn(secret, str(failure.exception))

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
            with self.assertRaisesRegex(FABLE.AdvisorError, "did not confirm"):
                FABLE.review_plan("packet")

    def test_each_operation_pins_its_authorized_seat_effort(self) -> None:
        self.write_state(planner=self.route("low"))
        created, create_calls = self.invoke_with_results(
            FABLE.create_plan, "packet", model_response="PLAN_DRAFT\nDraft"
        )
        self.write_state(advisor=self.route("xhigh"))
        reviewed, review_calls = self.invoke_with_results(
            FABLE.review_plan, "packet", model_response="PLAN_APPROVED\nGood"
        )
        self.assertEqual(created["effort"], "low")
        self.assertEqual(reviewed["effort"], "xhigh")
        create_command = create_calls[1][0]
        review_command = review_calls[1][0]
        self.assertEqual(create_command[create_command.index("--effort") + 1], "low")
        self.assertEqual(
            review_command[review_command.index("--effort") + 1], "xhigh"
        )
        self.assertEqual(
            create_command[create_command.index("--system-prompt") + 1],
            FABLE.PLANNER_CREATE_SYSTEM_PROMPT,
        )
        self.assertEqual(
            review_command[review_command.index("--system-prompt") + 1],
            FABLE.ADVISOR_SYSTEM_PROMPT,
        )

    def test_seat_authorization_does_not_cross_planner_and_advisor(self) -> None:
        self.write_state(planner=self.route())
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            with self.assertRaisesRegex(FABLE.AdvisorError, "configured advisor"):
                FABLE.review_plan("packet")

        self.write_state(advisor=self.route())
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            with self.assertRaisesRegex(FABLE.AdvisorError, "configured planner"):
                FABLE.create_plan("packet")
            with self.assertRaisesRegex(FABLE.AdvisorError, "configured planner"):
                FABLE.revise_plan("task", "v1 plan", "F-1", "history")

    def test_route_validation_is_constrained_and_backward_compatible(self) -> None:
        self.assertEqual(FABLE.load_fable_route(self.home)["effort"], "high")
        with self.assertRaisesRegex(FABLE.AdvisorError, "planner.*advisor"):
            FABLE.load_fable_route(self.home, seat="executor")

        invalid = self.route()
        invalid["server"] = "unmanaged-server"
        self.write_state(advisor=invalid)
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home)

        self.write_state(planner=self.route(), advisor=self.route("xhigh"))
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home, seat="planner")
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home, seat="advisor")

        self.write_state(schema=2, advisor=self.route())
        self.assertEqual(FABLE.load_fable_route(self.home)["effort"], "high")
        self.write_state(schema=2, planner=self.route())
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home, seat="planner")

        self.write_state(schema=4, advisor=self.route())
        self.assertEqual(FABLE.load_fable_route(self.home)["effort"], "high")

    def test_authorization_state_tampering_fails_before_any_subprocess(self) -> None:
        mutations = {
            "policy version": lambda payload: payload.update(policy_version=2),
            "other Codex home": lambda payload: payload.update(
                config_file=str(self.home / "other" / "config.toml")
            ),
            "wrong namespace": lambda payload: payload["managed"].update(
                namespace="collaboration"
            ),
            "unmarked policy": lambda payload: payload["managed"].update(
                mode="unmarked mode"
            ),
            "disabled launcher": lambda payload: payload["managed"]["mcp"].update(
                {"fable-advisor-python3": False}
            ),
        }
        state_path = self.home / FABLE.STATE_FILENAME
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.write_state(planner=self.route())
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                mutate(payload)
                state_path.write_text(json.dumps(payload), encoding="utf-8")
                with (
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
                    mock.patch.object(FABLE.subprocess, "run") as run,
                    self.assertRaises(FABLE.AdvisorError),
                ):
                    FABLE.create_plan("packet")
                run.assert_not_called()

        self.write_state(planner=self.route())
        sibling = self.home / "linked-routing-state.json"
        os.link(state_path, sibling)
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
            mock.patch.object(FABLE.subprocess, "run") as run,
            self.assertRaisesRegex(FABLE.AdvisorError, "multiple hard links"),
        ):
            FABLE.create_plan("packet")
        run.assert_not_called()

        sibling.unlink()
        self.write_state(planner=self.route())
        payload = json.loads((self.home / FABLE.STATE_FILENAME).read_text())
        payload.pop("managed_by")
        (self.home / FABLE.STATE_FILENAME).write_text(json.dumps(payload))
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home)

    def test_create_signal_success_and_failure(self) -> None:
        self.write_state(planner=self.route("medium"))
        result, _ = self.invoke_with_results(
            FABLE.create_plan,
            "complete packet",
            model_response="\nPLAN_DRAFT\n1. Verify inputs.",
        )
        self.assertEqual(result["signal"], "PLAN_DRAFT")
        self.assertIn("Verify inputs", result["plan"])

        with self.assertRaisesRegex(FABLE.AdvisorError, "PLAN_DRAFT"):
            self.invoke_with_results(
                FABLE.create_plan,
                "complete packet",
                model_response="Here is a draft.",
            )

    def test_revise_requires_all_inputs_and_structured_non_empty_sections(self) -> None:
        self.write_state(planner=self.route())
        for position in range(4):
            values: list[object] = ["task", "v1 plan", "F-1: fix", "prior ledger"]
            values[position] = " "
            with self.subTest(position=position):
                with self.assertRaisesRegex(FABLE.AdvisorError, "non-empty string"):
                    FABLE.revise_plan(*values)

        valid = (
            "PLAN_REVISION\n\n"
            "## FINDINGS_LEDGER\n"
            "- F-1 — INCORPORATED: add verification.\n\n"
            "## REVISED_PLAN\n"
            "Version: v2 (source v1)\n1. Add verification."
        )
        result, calls = self.invoke_with_results(
            FABLE.revise_plan,
            "original task",
            "Version v1\nplan",
            "F-1: missing verification",
            "F-0 incorporated",
            model_response=valid,
        )
        self.assertEqual(result["signal"], "PLAN_REVISION")
        self.assertIn("## REVISED_PLAN", result["revision"])
        prompt = calls[1][1]["input"]
        self.assertIn("# ORIGINAL_TASK", prompt)
        self.assertIn("# CANONICAL_CURRENT_PLAN_WITH_SOURCE_VERSION", prompt)
        self.assertIn("# LATEST_ADVISOR_CRITIQUE_WITH_STABLE_FINDING_IDS", prompt)
        self.assertIn("# COMPACT_CUMULATIVE_FINDINGS_HISTORY", prompt)

        malformed_responses = (
            "PLAN_DRAFT\n## FINDINGS_LEDGER\nF-1\n## REVISED_PLAN\nplan",
            "PLAN_REVISION\n## REVISED_PLAN\nplan",
            "PLAN_REVISION\n## FINDINGS_LEDGER\n\n## REVISED_PLAN\nplan",
            "PLAN_REVISION\n## FINDINGS_LEDGER\nF-1\n## REVISED_PLAN\n",
            (
                "PLAN_REVISION\n## REVISED_PLAN\nplan\n"
                "## FINDINGS_LEDGER\nF-1"
            ),
        )
        for response in malformed_responses:
            with self.subTest(response=response):
                with self.assertRaises(FABLE.AdvisorError):
                    self.invoke_with_results(
                        FABLE.revise_plan,
                        "task",
                        "v1 plan",
                        "F-1",
                        "history",
                        model_response=response,
                    )

    def test_repeated_revisions_are_fresh_and_never_use_sessions(self) -> None:
        self.write_state(planner=self.route())
        response = (
            "PLAN_REVISION\n## FINDINGS_LEDGER\n"
            "F-1 — INCORPORATED: reason\n## REVISED_PLAN\nv2 plan"
        )
        all_commands: list[list[str]] = []
        for _ in range(2):
            _, calls = self.invoke_with_results(
                FABLE.revise_plan,
                "task",
                "v1 plan",
                "F-1",
                "history",
                model_response=response,
            )
            all_commands.append(calls[1][0])
        self.assertEqual(len(all_commands), 2)
        for command in all_commands:
            self.assertEqual(command.count("--no-session-persistence"), 1)
            self.assertNotIn("--resume", command)
            self.assertNotIn("--session-id", command)

    def test_malformed_json_unconfirmed_model_and_bad_review_fail_closed(self) -> None:
        bad_outputs = (
            ("not json", "malformed JSON"),
            (
                json.dumps({"result": "PLAN_DRAFT\nDraft", "modelUsage": {}}),
                "did not confirm",
            ),
        )
        self.write_state(planner=self.route())
        for stdout, message in bad_outputs:
            with self.subTest(message=message):
                with (
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
                    mock.patch.object(
                        FABLE, "resolve_claude", return_value=Path("/fake/claude")
                    ),
                    mock.patch.object(
                        FABLE.subprocess,
                        "run",
                        side_effect=[
                            self.auth_result(),
                            self.completed(["claude"], stdout),
                        ],
                    ),
                ):
                    with self.assertRaisesRegex(FABLE.AdvisorError, message):
                        FABLE.create_plan("packet")

        self.write_state(advisor=self.route())
        result, _ = self.invoke_with_results(
            FABLE.review_plan, "packet", model_response="Looks good."
        )
        self.assertEqual(result["decision"], "PLAN_REVISE")

    def test_subprocess_failures_and_timeouts_do_not_leak_prompt_output(self) -> None:
        secret = "TOP-SECRET-PLAN-CONTENT"
        failed = self.completed(
            ["claude"],
            secret,
            returncode=17,
            stderr=f"provider error included {secret}",
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE, "resolve_claude", return_value=Path("/fake/claude")),
            mock.patch.object(
                FABLE.subprocess, "run", side_effect=[self.auth_result(), failed]
            ),
        ):
            with self.assertRaises(FABLE.AdvisorError) as failure:
                FABLE.review_plan(secret)
        self.assertIn("17", str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))

        timeout = subprocess.TimeoutExpired(["claude"], 600, output=secret, stderr=secret)
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(
                FABLE, "resolve_claude", return_value=Path("/fake/claude")
            ),
            mock.patch.object(
                FABLE.subprocess, "run", side_effect=[self.auth_result(), timeout]
            ),
        ):
            with self.assertRaises(FABLE.AdvisorError) as timed_out:
                FABLE.review_plan(secret)
        self.assertIn("timed out", str(timed_out.exception))
        self.assertNotIn(secret, str(timed_out.exception))

    def test_input_bound_is_checked_before_subprocess(self) -> None:
        with mock.patch.object(FABLE.subprocess, "run") as run:
            with self.assertRaisesRegex(FABLE.AdvisorError, "character combined limit"):
                FABLE.review_plan("x" * (FABLE.MAX_INPUT_CHARS + 1))
        run.assert_not_called()

        self.write_state(planner=self.route())
        oversized_piece = "x" * (FABLE.MAX_INPUT_CHARS // 2 + 1)
        with mock.patch.object(FABLE.subprocess, "run") as run:
            with self.assertRaisesRegex(FABLE.AdvisorError, "character combined limit"):
                FABLE.revise_plan(
                    oversized_piece, oversized_piece, "critique", "history"
                )
        run.assert_not_called()

    def test_mcp_surface_exposes_exact_bounded_tools_and_schemas(self) -> None:
        initialized = FABLE.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(
            initialized["result"]["serverInfo"]["name"],
            "codex-orchestration-fable-advisor",
        )
        listed = FABLE.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = listed["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["create_plan", "revise_plan", "review_plan", "status"],
        )
        for tool in tools:
            annotations = tool["annotations"]
            self.assertTrue(annotations["readOnlyHint"])
            self.assertFalse(annotations["destructiveHint"])
            self.assertTrue(annotations["idempotentHint"])
            self.assertTrue(annotations["openWorldHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["packet"])
        self.assertEqual(
            tools[1]["inputSchema"]["required"],
            ["task", "current_plan", "critique", "history"],
        )
        self.assertEqual(tools[2]["inputSchema"]["required"], ["packet"])
        for name in ("task", "current_plan", "critique", "history"):
            self.assertEqual(
                tools[1]["inputSchema"]["properties"][name]["maxLength"],
                FABLE.MAX_INPUT_CHARS,
            )

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
        self.assertEqual(result["configured_effort"], "high")
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
                "max_tokens": 131072,
                "system": FABLE.SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": "complete packet"}],
            },
        )
        self.assertNotIn("effort", body)
        self.assertNotIn("output_config", body)

    def test_config_file_direct_api_is_isolated_from_all_other_sources(self) -> None:
        self.set_direct_route("config-file")
        self.write_standalone_api_config()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="poison-settings-token",
            ANTHROPIC_BASE_URL="https://poison-settings.invalid",
        )
        poison_env = {
            "CODEX_HOME": str(self.home),
            "ANTHROPIC_API_KEY": "poison-env-key",
            "ANTHROPIC_AUTH_TOKEN": "poison-env-token",
            "ANTHROPIC_BASE_URL": "https://poison-env.invalid",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Poison: yes",
            "CLAUDE_CODE_OAUTH_TOKEN": "poison-oauth",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "poison-refresh",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "poison-model",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "poison-model",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "poison-model",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "poison-model",
        }
        opener = mock.Mock()
        opener.open.return_value = FakeHttpResponse(
            {
                "model": "anthropic/claude-fable-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "PLAN_APPROVED"}],
            }
        )
        with (
            mock.patch.dict(os.environ, poison_env, clear=True),
            mock.patch.object(FABLE.urllib_request, "build_opener", return_value=opener),
            mock.patch.object(
                FABLE,
                "user_settings_api_invocation",
                side_effect=AssertionError("config-file read user settings"),
            ) as user_settings,
            mock.patch.object(FABLE, "resolve_claude") as resolve_claude,
            mock.patch.object(FABLE.subprocess, "run") as run,
        ):
            result = FABLE.review_plan("standalone packet")

        self.assertEqual(result["decision"], "PLAN_APPROVED")
        self.assertEqual(result["api_source"], "config-file")
        self.assertEqual(result["request_model"], "anthropic/claude-fable-5")
        self.assertEqual(result["response_model"], "anthropic/claude-fable-5")
        self.assertEqual(result["model"], "claude-fable-5")
        self.assertEqual(result["used_models"], ["claude-fable-5"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/messages")
        self.assertEqual(json.loads(request.data)["model"], "anthropic/claude-fable-5")
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer standalone-secret")
        self.assertNotIn("poison", json.dumps(result))
        user_settings.assert_not_called()
        resolve_claude.assert_not_called()
        run.assert_not_called()
        opener.open.assert_called_once()

    def test_python_api_provider_model_is_mapped_but_advisor_remains_fable(self) -> None:
        self.set_direct_route("config-file")
        self.write_python_api_provider(api_key="provider-secret", model="x-ai/grok-4.5")
        opener = mock.Mock()
        opener.open.return_value = FakeHttpResponse(
            {
                "model": "x-ai/grok-4.5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "PLAN_APPROVED"}],
            }
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.home),
                    "ANTHROPIC_AUTH_TOKEN": "ignored-environment-secret",
                },
                clear=True,
            ),
            mock.patch.object(FABLE.urllib_request, "build_opener", return_value=opener),
        ):
            result = FABLE.review_plan("provider packet")

        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["model"], "x-ai/grok-4.5")
        self.assertEqual(result["request_model"], "x-ai/grok-4.5")
        self.assertEqual(result["response_model"], "x-ai/grok-4.5")
        self.assertEqual(result["model"], "claude-fable-5")
        self.assertEqual(result["used_models"], ["claude-fable-5"])
        self.assertEqual(result["advisor_path"], "python-api")

    def test_blank_python_api_key_disables_without_environment_fallback(self) -> None:
        self.set_direct_route("config-file")
        self.write_python_api_provider(api_key="")
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.home),
                    "ANTHROPIC_AUTH_TOKEN": "must-not-fallback",
                },
                clear=True,
            ),
            mock.patch.object(FABLE.urllib_request, "build_opener") as opener,
            self.assertRaisesRegex(FABLE.AdvisorError, "api_key is empty"),
        ):
            FABLE.review_plan("disabled packet")
        opener.assert_not_called()

    def test_python_api_model_echo_mismatch_fails_before_fable_metadata(self) -> None:
        self.set_direct_route("config-file")
        self.write_python_api_provider(api_key="provider-secret", model="x-ai/grok-4.5")
        opener = mock.Mock()
        opener.open.return_value = FakeHttpResponse(
            {
                "model": "x-ai/grok-4.5-versioned",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "PLAN_APPROVED"}],
            }
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(FABLE.urllib_request, "build_opener", return_value=opener),
            self.assertRaisesRegex(FABLE.AdvisorError, "requested model") as caught,
        ):
            FABLE.review_plan("mismatch packet")
        self.assertIn("x-ai/grok-4.5-versioned", str(caught.exception))
        self.assertNotIn("provider-secret", str(caught.exception))

    def test_saved_advisor_path_is_inferred_and_inconsistency_is_rejected(self) -> None:
        self.assertEqual(FABLE.load_fable_route(self.home)["path"], "claude-code-cli")
        self.set_direct_route("user-settings")
        self.assertEqual(FABLE.load_fable_route(self.home)["path"], "ccswitch")
        self.set_direct_route("config-file")
        self.assertEqual(FABLE.load_fable_route(self.home)["path"], "python-api")

        state_path = self.home / FABLE.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["advisor"]["path"] = "ccswitch"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"):
            FABLE.load_fable_route(self.home)

    def test_config_file_missing_or_invalid_after_routing_fails_closed(self) -> None:
        self.set_direct_route("config-file")
        for payload in (None, "not-json", json.dumps({"schema": 1})):
            with self.subTest(payload=payload):
                path = self.home / ".codex-orchestration-fable-api.json"
                path.unlink(missing_ok=True)
                if payload is not None:
                    path.write_text(payload, encoding="utf-8")
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "CODEX_HOME": str(self.home),
                            "ANTHROPIC_AUTH_TOKEN": "fallback-must-not-run",
                        },
                        clear=True,
                    ),
                    mock.patch.object(FABLE.urllib_request, "build_opener") as opener,
                    mock.patch.object(FABLE, "user_settings_api_invocation") as settings,
                    mock.patch.object(FABLE, "resolve_claude") as resolve_claude,
                    self.assertRaises(FABLE.AdvisorError) as caught,
                ):
                    FABLE.review_plan("packet")
                self.assertIn("configure_fable_api.py", str(caught.exception))
                self.assertNotIn("fallback-must-not-run", str(caught.exception))
                opener.assert_not_called()
                settings.assert_not_called()
                resolve_claude.assert_not_called()

    def test_config_file_source_requires_direct_api_transport(self) -> None:
        state_path = self.home / FABLE.STATE_FILENAME
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["advisor"].update(
            {
                "auth_mode": "api",
                "api_source": "config-file",
                "transport": "claude-code",
            }
        )
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"),
        ):
            FABLE.load_fable_route()

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

    def test_direct_api_refusal_reports_safe_details_and_stays_blocked(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        payload = {
            "model": "claude-fable-5",
            "stop_reason": "refusal",
            "stop_details": {
                "type": "refusal",
                "category": "cyber",
                "explanation": "  Classified as cyber.\nDo not retry unchanged.  ",
            },
            "content": [
                {"type": "text", "text": "PLAN_APPROVED secret-token"}
            ],
        }
        opener = mock.Mock()
        opener.open.return_value = FakeHttpResponse(payload)
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(
                FABLE.urllib_request, "build_opener", return_value=opener
            ),
            self.assertRaises(FABLE.AdvisorError) as raised,
        ):
            FABLE.review_plan("packet")

        message = str(raised.exception)
        self.assertIn("refusal_type='refusal'", message)
        self.assertIn("category='cyber'", message)
        self.assertIn(
            "explanation='Classified as cyber. Do not retry unchanged.'", message
        )
        self.assertIn("executor work must remain blocked", message)
        self.assertNotIn("PLAN_APPROVED", message)
        self.assertNotIn("secret-token", message)

    def test_direct_api_refusal_without_details_is_still_diagnostic(self) -> None:
        self.set_direct_route()
        self.write_user_api_settings(
            ANTHROPIC_AUTH_TOKEN="secret-token",
            ANTHROPIC_BASE_URL="http://127.0.0.1:15721",
        )
        opener = mock.Mock()
        opener.open.return_value = FakeHttpResponse(
            {
                "model": "claude-fable-5",
                "stop_reason": "refusal",
                "stop_details": None,
                "content": [],
            }
        )
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            mock.patch.object(
                FABLE.urllib_request, "build_opener", return_value=opener
            ),
            self.assertRaisesRegex(
                FABLE.AdvisorError,
                r"refusal_type=None; category=None; explanation=None",
            ),
        ):
            FABLE.review_plan("packet")

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
        self.assertEqual(status["configured_effort"], "high")
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
            self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"),
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
        self.assertIn(
            "outside the allowed Fable runtime policy",
            tool_result["content"][0]["text"],
        )

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
        self.assertEqual(
            initialized["result"]["serverInfo"]["name"],
            "codex-orchestration-fable-advisor",
        )
        listed = FABLE.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = listed["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["create_plan", "revise_plan", "review_plan", "status"],
        )
        for tool in tools:
            annotations = tool["annotations"]
            self.assertTrue(annotations["readOnlyHint"])
            self.assertFalse(annotations["destructiveHint"])
            self.assertTrue(annotations["idempotentHint"])
            self.assertTrue(annotations["openWorldHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["packet"])
        self.assertEqual(
            tools[1]["inputSchema"]["required"],
            ["task", "current_plan", "critique", "history"],
        )
        self.assertEqual(tools[2]["inputSchema"]["required"], ["packet"])
        for name in ("task", "current_plan", "critique", "history"):
            self.assertEqual(
                tools[1]["inputSchema"]["properties"][name]["maxLength"],
                FABLE.MAX_INPUT_CHARS,
            )

    def test_status_reports_planner_or_advisor_without_account_metadata(self) -> None:
        scenarios = (
            ({"planner": self.route("low")}, ["planner"]),
            ({"advisor": self.route("max")}, ["advisor"]),
        )
        for seats, expected in scenarios:
            with self.subTest(expected=expected):
                self.write_state(**seats)
                with (
                    mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}),
                    mock.patch.object(
                        FABLE,
                        "check_claude_auth",
                        return_value={
                            "auth_method": "claude.ai",
                            "api_provider": "firstParty",
                        },
                    ),
                ):
                    payload = FABLE.status()
                self.assertEqual(payload["configured_seats"], expected)
                self.assertEqual(list(payload["seats"]), expected)
                text = json.dumps(payload)
                self.assertNotIn("subscription", text.lower())
                self.assertNotIn("account_plan", text.lower())
                for seat in expected:
                    self.assertEqual(payload["seats"][seat]["model"], FABLE.FABLE_MODEL)
                    self.assertEqual(
                        payload["seats"][seat]["effort"], seats[seat]["effort"]
                    )
                if "advisor" in expected:
                    self.assertEqual(payload["effort"], seats["advisor"]["effort"])

        self.write_state(planner=self.route(), advisor=self.route("xhigh"))
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
            self.assertRaisesRegex(FABLE.AdvisorError, "state is invalid"),
        ):
            FABLE.status()

    def test_status_tool_and_argument_validation_fail_closed(self) -> None:
        with (
            mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}, clear=True),
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

        extra = FABLE.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {"secret": "x"}},
            }
        )
        self.assertTrue(extra["result"]["isError"])
        self.assertIn("Unexpected tool argument", extra["result"]["content"][0]["text"])

    def test_saved_xhigh_and_legacy_max_efforts_remain_valid(self) -> None:
        for effort in ("xhigh", "max"):
            with self.subTest(effort=effort):
                self.write_state(advisor=self.route(effort))
                self.assertEqual(FABLE.load_fable_route(self.home)["effort"], effort)


if __name__ == "__main__":
    unittest.main()
