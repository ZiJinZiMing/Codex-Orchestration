from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "codex-orchestration" / "skills" / "codex-orchestration" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import configure_designer_api as DESIGNER  # noqa: E402


def config(api_key: str = "secret") -> dict[str, object]:
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


class DesignerApiConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = DESIGNER.config_path(self.home)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, value: dict[str, object]) -> None:
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_default_kimi_contract_and_nonsecret_fingerprint(self) -> None:
        value = DESIGNER.validate_config(config())
        self.assertTrue(value["enabled"])
        self.assertEqual(value["provider"]["model"], "k3")
        first = DESIGNER.config_sha256(value)
        changed = deepcopy(config("rotated"))
        self.assertEqual(first, DESIGNER.config_sha256(DESIGNER.validate_config(changed)))
        changed["provider"]["model"] = "other"
        self.assertNotEqual(first, DESIGNER.config_sha256(DESIGNER.validate_config(changed)))

    def test_init_default_and_status_never_print_secret(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(DESIGNER.main(["--codex-home", str(self.home), "--init-default"]), 0)
            self.assertEqual(DESIGNER.main(["--codex-home", str(self.home), "--status"]), 0)
        text = stdout.getvalue()
        self.assertNotIn("api_key", text)
        self.assertIn('"model_call":false', text)
        self.assertFalse(DESIGNER.load_config(self.path)["enabled"])

    def test_api_key_is_rejected_on_argv(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(DESIGNER.main(["--api-key=must-not-print"]), 1)
        self.assertNotIn("must-not-print", stderr.getvalue())

    def test_strict_shape_and_duplicate_fields(self) -> None:
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.update(role="advisor"),
            lambda value: value["provider"].update(extra=True),
            lambda value: value["provider"].pop("model"),
        ):
            value = config()
            mutate(value)
            with self.assertRaises(DESIGNER.DesignerApiConfigError):
                DESIGNER.validate_config(value)
        self.path.write_text('{"schema":1,"schema":1,"role":"designer","provider":{}}', encoding="utf-8")
        with self.assertRaisesRegex(DESIGNER.DesignerApiConfigError, "duplicate"):
            DESIGNER.load_config(self.path)

    def test_provider_url_auth_protocol_model_and_token_validation(self) -> None:
        cases = [
            ("id", "Bad.Provider"),
            ("api_url", "http://example.com/v1/messages"),
            ("api_url", "https://user:pass@example.com/v1/messages"),
            ("api_url", "https://example.com/v1/chat/completions"),
            ("api_key", "bad\nheader"),
            ("model", "k3 latest"),
            ("auth_type", "api-key"),
            ("wire_api", "responses"),
            ("max_tokens", True),
            ("max_tokens", 0),
            ("max_tokens", 65537),
        ]
        for field, bad in cases:
            with self.subTest(field=field, bad=bad):
                value = config()
                value["provider"][field] = bad
                with self.assertRaises(DESIGNER.DesignerApiConfigError):
                    DESIGNER.validate_config(value)

    def test_file_type_symlink_and_hardlink_fail_closed(self) -> None:
        self.path.mkdir()
        with self.assertRaisesRegex(DESIGNER.DesignerApiConfigError, "regular file"):
            DESIGNER.load_config(self.path)
        self.path.rmdir()
        target = self.home / "target.json"
        target.write_text(json.dumps(config()), encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except OSError:
            pass
        else:
            with self.assertRaisesRegex(DESIGNER.DesignerApiConfigError, "regular file"):
                DESIGNER.load_config(self.path)
            self.path.unlink()
        try:
            import os
            os.link(target, self.path)
        except OSError:
            return
        with self.assertRaisesRegex(DESIGNER.DesignerApiConfigError, "hard links"):
            DESIGNER.load_config(self.path)


if __name__ == "__main__":
    unittest.main()
