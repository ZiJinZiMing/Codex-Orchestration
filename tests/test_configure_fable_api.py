from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
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
    / "configure_fable_api.py"
)
SPEC = importlib.util.spec_from_file_location("configure_fable_api", SCRIPT)
assert SPEC and SPEC.loader
FABLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FABLE)


def valid_config(credential: str = "secret-token") -> dict[str, object]:
    return {
        "schema": 1,
        "api_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-fable-5",
        "auth_type": "x-api-key",
        "credential": credential,
    }


def provider_config(
    api_key: str = "",
    *,
    api_url: str = "https://openrouter.ai/api/v1/messages",
    model: str = "anthropic/claude-fable-5",
    auth_type: str = "bearer",
) -> dict[str, object]:
    return {
        "schema": 2,
        "provider": {
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "auth_type": auth_type,
        },
    }


def normalized_provider(data: dict[str, object], *, legacy: bool) -> dict[str, object]:
    provider = data["provider"]
    assert isinstance(provider, dict)
    return {
        "schema": 1 if legacy else 2,
        "provider": provider,
        "enabled": bool(provider["api_key"]),
        "legacy": legacy,
    }


class ConfigureFableApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex-home"
        self.path = self.home / FABLE.CONFIG_FILENAME

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, payload: object) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def run_main(
        self,
        argv: list[str],
        *,
        stdin: str = "",
        input_values: list[str] | None = None,
        credential: str = "secret-token",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        patches = [
            mock.patch.object(FABLE.sys, "stdin", io.StringIO(stdin)),
            mock.patch.object(FABLE.getpass, "getpass", return_value=credential),
        ]
        if input_values is not None:
            patches.append(mock.patch("builtins.input", side_effect=input_values))
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            result = FABLE.main(["--codex-home", str(self.home), *argv])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_interactive_defaults_use_input_and_getpass_without_printing_secret(self) -> None:
        result, output, error = self.run_main(
            [], input_values=["", "", ""], credential="do-not-print"
        )
        self.assertEqual(result, 0, error)
        self.assertIn(str(self.path), output)
        self.assertNotIn("do-not-print", output)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            provider_config("do-not-print"),
        )

    def test_credential_stdin_reads_one_line_and_never_accepts_argv_secret(self) -> None:
        result, output, error = self.run_main(
            [
                "--api-url",
                "http://localhost:8080/v1/messages",
                "--model",
                "anthropic/claude-fable-5",
                "--auth-type",
                "bearer",
                "--credential-stdin",
            ],
            stdin="stdin-secret\nignored\n",
        )
        self.assertEqual(result, 0, error)
        self.assertNotIn("stdin-secret", output)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            provider_config(
                "stdin-secret",
                api_url="http://localhost:8080/v1/messages",
            ),
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            FABLE.parse_args(["--credential", "secret"])
        result, output, error = self.run_main(["--credential", "argv-secret"])
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertNotIn("argv-secret", error)

    def test_empty_credential_stdin_fails(self) -> None:
        result, output, error = self.run_main(
            [
                "--api-url",
                "https://api.anthropic.com/v1/messages",
                "--model",
                "claude-fable-5",
                "--auth-type",
                "x-api-key",
                "--credential-stdin",
            ],
            stdin="\n",
        )
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("credential", error.lower())
        self.assertNotIn("secret", error.lower())

    @unittest.skipUnless(os.name != "nt", "POSIX file mode semantics")
    def test_atomic_write_sets_private_mode(self) -> None:
        result, _, error = self.run_main(
            [
                "--api-url",
                FABLE.DEFAULT_API_URL,
                "--model",
                FABLE.DEFAULT_MODEL,
                "--auth-type",
                FABLE.DEFAULT_AUTH_TYPE,
                "--credential-stdin",
            ],
            stdin="mode-secret\n",
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertFalse(any(self.home.glob(f".{self.path.name}.*.tmp")))

    def test_overwrite_guard_and_force(self) -> None:
        self.write(valid_config("old-secret"))
        result, output, error = self.run_main([], credential="new-secret")
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("--force", error)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["credential"], "old-secret")

        result, output, error = self.run_main(
            [
                "--force",
                "--api-url",
                "https://api.anthropic.com/v1/messages",
                "--model",
                "claude-fable-5",
                "--auth-type",
                "x-api-key",
                "--credential-stdin",
            ],
            stdin="new-secret\n",
        )
        self.assertEqual(result, 0, error)
        self.assertNotIn("new-secret", output)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["provider"]["api_key"],
            "new-secret",
        )

    def test_write_failure_is_reported_without_traceback(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        blocking_home = self.home / "blocking-home"
        blocking_home.write_text("not a directory", encoding="utf-8")
        result, output, error = self.run_main(
            [
                "--codex-home",
                str(blocking_home / "nested"),
                "--api-url",
                "https://api.anthropic.com/v1/messages",
                "--model",
                "claude-fable-5",
                "--auth-type",
                "x-api-key",
                "--credential-stdin",
            ],
            stdin="write-secret\n",
        )
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertIn("Could not create", error)
        self.assertNotIn("write-secret", error)

    def test_status_outputs_only_non_secret_metadata(self) -> None:
        self.write(valid_config("status-secret"))
        result, output, error = self.run_main(["--status"])
        self.assertEqual(result, 0, error)
        metadata = json.loads(output)
        self.assertEqual(
            set(metadata),
            {
                "available",
                "enabled",
                "schema",
                "advisor_path",
                "model",
                "auth_type",
                "legacy",
                "path",
            },
        )
        self.assertTrue(metadata["available"])
        self.assertEqual(metadata["path"], str(self.path))
        self.assertNotIn("status-secret", output)
        self.assertNotIn("Authorization: Bearer", output)

    def test_status_invalid_config_is_nonzero_and_does_not_leak_secret(self) -> None:
        self.write({**valid_config("invalid-status-secret"), "unknown": "value"})
        result, output, error = self.run_main(["--status"])
        self.assertEqual(result, 1)
        self.assertEqual(output, "")
        self.assertNotIn("invalid-status-secret", error)

    def test_load_config_accepts_explicit_path_and_codex_home(self) -> None:
        self.write(valid_config())
        expected = normalized_provider(
            {
                "provider": {
                    "api_url": "https://api.anthropic.com/v1/messages",
                    "api_key": "secret-token",
                    "model": "claude-fable-5",
                    "auth_type": "x-api-key",
                }
            },
            legacy=True,
        )
        self.assertEqual(FABLE.load_config(self.path), expected)
        self.assertEqual(FABLE.load_config(codex_home=self.home), expected)
        self.assertEqual(FABLE.config_path(self.home), self.path)

    def test_missing_config_has_initializer_hint(self) -> None:
        with self.assertRaisesRegex(FABLE.FableApiConfigError, "configure_fable_api.py"):
            FABLE.load_config(self.path)

    def test_missing_positional_codex_home_uses_default_filename(self) -> None:
        missing_home = self.home / ".codex-missing"
        with self.assertRaisesRegex(
            FABLE.FableApiConfigError,
            FABLE.CONFIG_FILENAME,
        ):
            FABLE.load_config(missing_home)

    def test_invalid_json_and_strict_schema(self) -> None:
        self.home.mkdir(parents=True)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(FABLE.FableApiConfigError):
            FABLE.load_config(self.path)

        invalid_payloads = [
            [],
            {**valid_config(), "unknown": True},
            {key: value for key, value in valid_config().items() if key != "credential"},
            {**valid_config(), "schema": True},
            {**valid_config(), "schema": 1.0},
            {**valid_config(), "credential": 3},
            {**valid_config(), "credential": "   "},
        ]
        for payload in invalid_payloads:
            self.write(payload)
            with self.assertRaises(FABLE.FableApiConfigError):
                FABLE.load_config(self.path)

        self.path.write_text(
            '{"schema":1,"api_url":"https://api.anthropic.com/v1/messages",'
            '"model":"claude-fable-5","auth_type":"x-api-key",'
            '"credential":"a","credential":"b"}',
            encoding="utf-8",
        )
        with self.assertRaises(FABLE.FableApiConfigError):
            FABLE.load_config(self.path)

    def test_url_validation(self) -> None:
        good = [
            "https://api.anthropic.com/v1/messages",
            "https://gateway.example.test/prefix/v1/messages",
            "http://localhost/v1/messages",
            "http://127.0.0.1:8080/v1/messages",
            "http://[::1]/v1/messages",
        ]
        for url in good:
            payload = {**valid_config(), "api_url": url}
            self.write(payload)
            self.assertEqual(FABLE.load_config(self.path)["provider"]["api_url"], url)

        bad = [
            "http://api.anthropic.com/v1/messages",
            "ftp://api.anthropic.com/v1/messages",
            "https:///v1/messages",
            "https://user:password@example.test/v1/messages",
            "https://example.test/v1/messages?x=1",
            "https://example.test/v1/messages?",
            "https://example.test/v1/messages#fragment",
            "https://example.test/v1/messages#",
            "https://example.test/v1/messages/",
            "https://example.test/v1/chat/completions",
            "https://example.test:bad/v1/messages",
            "https://example.test:/v1/messages",
        ]
        for url in bad:
            self.write({**valid_config(), "api_url": url})
            with self.assertRaises(FABLE.FableApiConfigError, msg=url):
                FABLE.load_config(self.path)

    def test_model_and_auth_type_are_exact_allowlists(self) -> None:
        for model in ("claude-fable-5 ", "Claude-Fable-5", "other"):
            self.write({**valid_config(), "model": model})
            with self.assertRaises(FABLE.FableApiConfigError):
                FABLE.load_config(self.path)
        for auth_type in ("Bearer", "api-key", ""):
            self.write({**valid_config(), "auth_type": auth_type})
            with self.assertRaises(FABLE.FableApiConfigError):
                FABLE.load_config(self.path)

    def test_init_default_creates_disabled_python_api_provider(self) -> None:
        result, output, error = self.run_main(["--init-default"])
        self.assertEqual(result, 0, error)
        self.assertIn("disabled Python API", output)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), provider_config())

        result, status, error = self.run_main(["--status"])
        self.assertEqual(result, 0, error)
        metadata = json.loads(status)
        self.assertFalse(metadata["enabled"])
        self.assertFalse(metadata["available"])
        self.assertEqual(metadata["advisor_path"], "python-api")
        self.assertNotIn("api_key", status)
        self.assertNotIn("api_url", status)

    def test_provider_model_is_configurable_but_strictly_formed(self) -> None:
        self.write(provider_config("key", model="x-ai/grok-4.5"))
        loaded = FABLE.load_config(self.path)
        self.assertEqual(loaded["provider"]["model"], "x-ai/grok-4.5")
        self.assertTrue(loaded["enabled"])

        for model in ("", " leading", "trailing ", "two words", "x\nmodel"):
            self.write(provider_config("key", model=model))
            with self.assertRaises(FABLE.FableApiConfigError, msg=repr(model)):
                FABLE.load_config(self.path)

    def test_legacy_schema_is_normalized_without_rewriting(self) -> None:
        self.write(valid_config("legacy-secret"))
        before = self.path.read_bytes()
        loaded = FABLE.load_config(self.path)
        self.assertTrue(loaded["legacy"])
        self.assertEqual(loaded["provider"]["api_key"], "legacy-secret")
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_and_nonregular_files_are_rejected(self) -> None:
        self.home.mkdir(parents=True)
        target = self.home / "target.json"
        target.write_text(json.dumps(valid_config()), encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(FABLE.FableApiConfigError):
            FABLE.load_config(self.path)

        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(FABLE.FableApiConfigError):
            FABLE.load_config(self.path)


if __name__ == "__main__":
    unittest.main()
