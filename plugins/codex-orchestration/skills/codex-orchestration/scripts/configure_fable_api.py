#!/usr/bin/env python3
"""Initialize and validate the local Claude Fable direct-API credential file.

The file deliberately contains only the small, strict configuration needed by
the bundled direct API bridge.  Credentials are never accepted as a command
line argument and are omitted from all status and success output.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit


CONFIG_FILENAME = ".codex-orchestration-fable-api.json"
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
FABLE_MODEL = "claude-fable-5"
LEGACY_ALLOWED_MODELS = frozenset({FABLE_MODEL, f"anthropic/{FABLE_MODEL}"})
ALLOWED_AUTH_TYPES = frozenset({"bearer", "x-api-key"})
DEFAULT_API_URL = "https://openrouter.ai/api/v1/messages"
DEFAULT_MODEL = f"anthropic/{FABLE_MODEL}"
DEFAULT_AUTH_TYPE = "bearer"
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LEGACY_CONFIG_FIELDS = frozenset(
    {"schema", "api_url", "model", "auth_type", "credential"}
)
CONFIG_FIELDS = frozenset({"schema", "provider"})
PROVIDER_FIELDS = frozenset({"api_url", "api_key", "model", "auth_type"})
__all__ = [
    "ALLOWED_AUTH_TYPES",
    "CONFIG_FILENAME",
    "DEFAULT_API_URL",
    "DEFAULT_MODEL",
    "FableApiConfigError",
    "config_path",
    "load_config",
    "validate_config",
]


class FableApiConfigError(RuntimeError):
    """Raised when the direct-API configuration is missing or invalid."""


def codex_home(value: Path | str | None = None) -> Path:
    """Return the configured CODEX_HOME, or the standard user directory."""

    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def config_path(
    codex_home_path: Path | str | None = None,
    *,
    codex_home: Path | str | None = None,
) -> Path:
    """Return the direct-API configuration path for a Codex home.

    ``codex_home`` is accepted as a keyword for callers that use the same
    terminology as :func:`load_config`; the positional form remains concise.
    """

    if codex_home_path is not None and codex_home is not None:
        raise FableApiConfigError("Specify either a path or codex_home, not both.")
    return globals()["codex_home"](
        codex_home if codex_home is not None else codex_home_path
    ) / CONFIG_FILENAME


def _initializer_hint(path: Path) -> str:
    script = Path(__file__).resolve()
    return (
        f'Run `{sys.executable} "{script}" --codex-home "{path.parent}"` '
        "to initialize it."
    )


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FableApiConfigError("Configuration contains duplicate JSON fields.")
        result[key] = value
    return result


def _validate_api_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FableApiConfigError("api_url must be a complete Messages endpoint URL.")
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise FableApiConfigError("api_url must be a complete Messages endpoint URL.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise FableApiConfigError("api_url is not a valid URL.") from exc
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise FableApiConfigError(
            "api_url must not contain userinfo, query, or fragment components."
        )
    scheme = parsed.scheme.lower()
    hostname = hostname.lower()
    if scheme == "http":
        if hostname not in LOCAL_HTTP_HOSTS:
            raise FableApiConfigError(
                "api_url must use HTTPS except for exact localhost, 127.0.0.1, or ::1."
            )
    elif scheme != "https":
        raise FableApiConfigError("api_url must use HTTPS.")
    if not parsed.path.endswith("/v1/messages"):
        raise FableApiConfigError(
            "api_url path must end with /v1/messages."
        )
    return value


def _validate_model(value: Any, *, legacy: bool = False) -> str:
    if legacy:
        if not isinstance(value, str) or value not in LEGACY_ALLOWED_MODELS:
            raise FableApiConfigError(
                "legacy model must be claude-fable-5 or anthropic/claude-fable-5."
            )
        return value
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise FableApiConfigError(
            "provider model must be a non-empty printable identifier of at most 256 characters."
        )
    return value


def _validate_auth_type(value: Any) -> str:
    if not isinstance(value, str) or value not in ALLOWED_AUTH_TYPES:
        raise FableApiConfigError("auth_type must be bearer or x-api-key.")
    return value


def _validate_api_key(value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        if allow_empty and value == "":
            return value
        raise FableApiConfigError("api_key must be a non-empty string when enabled.")
    if len(value) > 8192 or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise FableApiConfigError(
            "api_key must not contain control characters and must be at most 8192 characters."
        )
    return value


def validate_config(data: Any) -> dict[str, Any]:
    """Validate and normalize a legacy or provider configuration object."""

    if not isinstance(data, dict):
        raise FableApiConfigError("Configuration root must be a JSON object.")
    schema = data.get("schema")
    if type(schema) is not int or schema not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise FableApiConfigError("schema must be the integer 1 or 2.")
    expected = LEGACY_CONFIG_FIELDS if schema == LEGACY_SCHEMA_VERSION else CONFIG_FIELDS
    keys = set(data)
    missing = expected - keys
    unknown = keys - expected
    if missing:
        raise FableApiConfigError(
            "Configuration is missing required field(s): "
            + ", ".join(sorted(missing))
            + "."
        )
    if unknown:
        raise FableApiConfigError(
            "Configuration contains unknown field(s): "
            + ", ".join(sorted(unknown))
            + "."
        )
    if schema == LEGACY_SCHEMA_VERSION:
        api_key = _validate_api_key(data["credential"], allow_empty=False)
        return {
            "schema": schema,
            "provider": {
                "api_url": _validate_api_url(data["api_url"]),
                "api_key": api_key,
                "model": _validate_model(data["model"], legacy=True),
                "auth_type": _validate_auth_type(data["auth_type"]),
            },
            "enabled": True,
            "legacy": True,
        }
    provider = data["provider"]
    if not isinstance(provider, dict):
        raise FableApiConfigError("provider must be a JSON object.")
    provider_keys = set(provider)
    provider_missing = PROVIDER_FIELDS - provider_keys
    provider_unknown = provider_keys - PROVIDER_FIELDS
    if provider_missing:
        raise FableApiConfigError(
            "Provider configuration is missing required field(s): "
            + ", ".join(sorted(provider_missing))
            + "."
        )
    if provider_unknown:
        raise FableApiConfigError(
            "Provider configuration contains unknown field(s): "
            + ", ".join(sorted(provider_unknown))
            + "."
        )
    api_key = _validate_api_key(provider["api_key"], allow_empty=True)
    return {
        "schema": SCHEMA_VERSION,
        "provider": {
            "api_url": _validate_api_url(provider["api_url"]),
            "api_key": api_key,
            "model": _validate_model(provider["model"]),
            "auth_type": _validate_auth_type(provider["auth_type"]),
        },
        "enabled": bool(api_key),
        "legacy": False,
    }


def load_config(
    path: Path | str | None = None,
    *,
    codex_home: Path | str | None = None,
) -> dict[str, Any]:
    """Read, strictly validate, and return a direct-API configuration.

    Pass either an explicit ``path`` or ``codex_home``.  For compatibility with
    the bridge, an existing directory passed positionally is treated as a
    Codex home.  A symlink or any non-regular file is rejected before reading
    to avoid following an unexpected credential path.
    """

    if path is not None and codex_home is not None:
        raise FableApiConfigError("Specify either path or codex_home, not both.")
    if path is not None:
        candidate = Path(path).expanduser()
        # The bridge's reusable call site passes CODEX_HOME positionally.  A
        # directory is unambiguous; a missing path without a file extension is
        # likewise treated as a Codex home so the error names the config file.
        positional_home = candidate.is_dir() or (
            not candidate.exists()
            and candidate.name != CONFIG_FILENAME
            and candidate.suffix.lower() not in {".json", ".toml"}
        )
        target = (
            candidate / CONFIG_FILENAME
            if positional_home and candidate.name != CONFIG_FILENAME
            else candidate
        )
    else:
        target = config_path(codex_home=codex_home)
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise FableApiConfigError(
            f"Could not inspect Fable API configuration at {target}. "
            f"{_initializer_hint(target)}"
        ) from exc
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise FableApiConfigError(
            f"Configuration path is a symlink: {target}. {_initializer_hint(target)}"
        )
    if info is None:
        raise FableApiConfigError(
            f"Fable API configuration is missing at {target}. {_initializer_hint(target)}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise FableApiConfigError(
            f"Fable API configuration is not a regular file: {target}. "
            f"{_initializer_hint(target)}"
        )
    if info.st_nlink != 1:
        raise FableApiConfigError(
            f"Fable API configuration has multiple hard links: {target}. "
            f"{_initializer_hint(target)}"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FableApiConfigError(
            f"Could not read Fable API configuration at {target}. "
            f"{_initializer_hint(target)}"
        ) from exc
    try:
        data = json.loads(text, object_pairs_hook=_strict_object_pairs)
    except FableApiConfigError as exc:
        raise FableApiConfigError(
            f"Invalid Fable API configuration at {target}: {exc} "
            f"{_initializer_hint(target)}"
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FableApiConfigError(
            f"Fable API configuration at {target} is not valid JSON. "
            f"{_initializer_hint(target)}"
        ) from exc
    try:
        return validate_config(data)
    except FableApiConfigError as exc:
        raise FableApiConfigError(
            f"Invalid Fable API configuration at {target}: {exc} "
            f"{_initializer_hint(target)}"
        ) from exc


def _existing_path(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FableApiConfigError(
            f"Could not create the Fable API configuration directory for {path}."
        ) from exc
    payload = json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    try:
                        os.fsync(directory_fd)
                    except OSError:
                        pass
                finally:
                    os.close(directory_fd)
    except OSError as exc:
        raise FableApiConfigError(f"Could not write Fable API configuration at {path}.") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _metadata(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    provider = data["provider"]
    return {
        "available": data["enabled"],
        "enabled": data["enabled"],
        "schema": data["schema"],
        "advisor_path": "python-api",
        "model": provider["model"],
        "auth_type": provider["auth_type"],
        "legacy": data["legacy"],
        "path": str(path),
    }


def _prompt(value: str | None, label: str, default: str) -> str:
    if value is not None:
        return value
    try:
        entered = input(f"{label} [{default}]: ")
    except (EOFError, OSError) as exc:
        raise FableApiConfigError(f"Could not read {label} from input.") from exc
    return entered.strip() or default


def _read_credential_from_stdin() -> str:
    try:
        value = sys.stdin.readline()
    except (OSError, UnicodeError) as exc:
        raise FableApiConfigError("Could not read credential from stdin.") from exc
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or not value.strip():
        raise FableApiConfigError("credential-stdin must provide a non-empty value.")
    return value


def _read_credential_interactively() -> str:
    try:
        value = getpass.getpass("Fable API credential: ")
    except (EOFError, OSError) as exc:
        raise FableApiConfigError("Could not read the Fable API credential.") from exc
    if not value or not value.strip():
        raise FableApiConfigError("Credential must be a non-empty value.")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize the local Claude Fable direct-API configuration."
    )
    parser.add_argument("--codex-home", type=Path, help="Override CODEX_HOME.")
    parser.add_argument("--api-url", help="Complete Anthropic Messages endpoint URL.")
    parser.add_argument("--model")
    parser.add_argument("--auth-type", choices=sorted(ALLOWED_AUTH_TYPES))
    parser.add_argument(
        "--api-key-stdin",
        "--credential-stdin",
        dest="credential_stdin",
        action="store_true",
        help="Read one API key line from stdin instead of prompting securely.",
    )
    parser.add_argument(
        "--init-default",
        action="store_true",
        help="Create a disabled default provider configuration with an empty API key.",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing file.")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Validate the saved configuration and print non-secret metadata.",
    )
    return parser.parse_args(argv)


def _initialize(args: argparse.Namespace, path: Path) -> int:
    if _existing_path(path) and not args.force:
        raise FableApiConfigError(
            f"Fable API configuration already exists at {path}; use --force to replace it."
        )
    api_url = _prompt(args.api_url, "API URL", DEFAULT_API_URL)
    model = _prompt(args.model, "Model", DEFAULT_MODEL)
    auth_type = _prompt(args.auth_type, "Auth type", DEFAULT_AUTH_TYPE)
    api_key = (
        _read_credential_from_stdin()
        if args.credential_stdin
        else _read_credential_interactively()
    )
    data = {
        "schema": SCHEMA_VERSION,
        "provider": {
            "api_url": api_url,
            "api_key": api_key,
            "model": model,
            "auth_type": auth_type,
        },
    }
    validate_config(data)
    _atomic_write(path, data)
    print(f"Configured Fable direct API at {path}")
    return 0


def _initialize_default(args: argparse.Namespace, path: Path) -> int:
    if _existing_path(path) and not args.force:
        raise FableApiConfigError(
            f"Fable API configuration already exists at {path}; use --force to replace it."
        )
    data = {
        "schema": SCHEMA_VERSION,
        "provider": {
            "api_url": DEFAULT_API_URL,
            "api_key": "",
            "model": DEFAULT_MODEL,
            "auth_type": DEFAULT_AUTH_TYPE,
        },
    }
    validate_config(data)
    _atomic_write(path, data)
    print(f"Created disabled Python API provider configuration at {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if any(
        argument in {"--credential", "--api-key"}
        or argument.startswith("--credential=")
        or argument.startswith("--api-key=")
        for argument in raw_argv
    ):
        print(
            "Error: API key must be supplied through getpass or --api-key-stdin.",
            file=sys.stderr,
        )
        return 1
    try:
        args = parse_args(raw_argv)
        path = config_path(args.codex_home)
        if args.status:
            data = load_config(path)
            print(json.dumps(_metadata(path, data), ensure_ascii=True, separators=(",", ":")))
            return 0
        if args.init_default:
            if any((args.api_url, args.model, args.auth_type, args.credential_stdin)):
                raise FableApiConfigError(
                    "--init-default cannot be combined with provider or API-key inputs."
                )
            return _initialize_default(args, path)
        return _initialize(args, path)
    except FableApiConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
