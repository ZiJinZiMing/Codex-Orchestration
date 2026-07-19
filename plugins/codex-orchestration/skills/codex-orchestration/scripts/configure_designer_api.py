#!/usr/bin/env python3
"""Initialize and validate the local direct-API Designer configuration."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit


CONFIG_FILENAME = ".codex-orchestration-designer-api.json"
SCHEMA_VERSION = 1
ROLE = "designer"
WIRE_API = "anthropic-messages"
DEFAULT_PROVIDER_ID = "kimi"
DEFAULT_API_URL = "https://api.kimi.com/coding/v1/messages"
DEFAULT_MODEL = "k3"
DEFAULT_AUTH_TYPE = "bearer"
DEFAULT_MAX_TOKENS = 16384
MAX_MAX_TOKENS = 65536
ALLOWED_AUTH_TYPES = frozenset({"bearer", "x-api-key"})
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
CONFIG_FIELDS = frozenset({"schema", "role", "provider"})
PROVIDER_FIELDS = frozenset(
    {"id", "api_url", "api_key", "model", "auth_type", "wire_api", "max_tokens"}
)
PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class DesignerApiConfigError(RuntimeError):
    """The direct-API Designer configuration is missing or invalid."""


def codex_home(value: Path | str | None = None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def config_path(value: Path | str | None = None) -> Path:
    return codex_home(value) / CONFIG_FILENAME


def _initializer_hint(path: Path) -> str:
    script = Path(__file__).resolve()
    return f'Run `{sys.executable} "{script}" --codex-home "{path.parent}"` to initialize it.'


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DesignerApiConfigError("Configuration contains duplicate JSON fields.")
        result[key] = value
    return result


def _validate_identifier(value: Any, *, label: str, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise DesignerApiConfigError(
            f"{label} must be a non-empty printable identifier of at most {limit} characters."
        )
    return value


def _validate_api_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DesignerApiConfigError("api_url must be a complete Messages endpoint URL.")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise DesignerApiConfigError("api_url must be a complete Messages endpoint URL.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise DesignerApiConfigError("api_url is not a valid URL.") from exc
    if (
        not parsed.scheme
        or not parsed.netloc
        or parsed.netloc.endswith(":")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DesignerApiConfigError(
            "api_url must not contain userinfo, query, or fragment components."
        )
    scheme = parsed.scheme.lower()
    hostname = hostname.lower()
    if scheme == "http":
        if hostname not in LOCAL_HTTP_HOSTS:
            raise DesignerApiConfigError(
                "api_url must use HTTPS except for exact localhost, 127.0.0.1, or ::1."
            )
    elif scheme != "https":
        raise DesignerApiConfigError("api_url must use HTTPS.")
    if not parsed.path.endswith("/v1/messages"):
        raise DesignerApiConfigError("api_url path must end with /v1/messages.")
    return value


def _validate_api_key(value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        if allow_empty and value == "":
            return value
        raise DesignerApiConfigError("api_key must be a non-empty string when enabled.")
    if len(value) > 8192 or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise DesignerApiConfigError(
            "api_key must not contain control characters and must be at most 8192 characters."
        )
    return value


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DesignerApiConfigError("Configuration root must be a JSON object.")
    if type(data.get("schema")) is not int or data["schema"] != SCHEMA_VERSION:
        raise DesignerApiConfigError("schema must be the integer 1.")
    missing = CONFIG_FIELDS - set(data)
    unknown = set(data) - CONFIG_FIELDS
    if missing:
        raise DesignerApiConfigError(
            "Configuration is missing required field(s): " + ", ".join(sorted(missing)) + "."
        )
    if unknown:
        raise DesignerApiConfigError(
            "Configuration contains unknown field(s): " + ", ".join(sorted(unknown)) + "."
        )
    if data["role"] != ROLE:
        raise DesignerApiConfigError("role must be exactly designer.")
    provider = data["provider"]
    if not isinstance(provider, dict):
        raise DesignerApiConfigError("provider must be a JSON object.")
    missing = PROVIDER_FIELDS - set(provider)
    unknown = set(provider) - PROVIDER_FIELDS
    if missing:
        raise DesignerApiConfigError(
            "Provider configuration is missing required field(s): "
            + ", ".join(sorted(missing))
            + "."
        )
    if unknown:
        raise DesignerApiConfigError(
            "Provider configuration contains unknown field(s): "
            + ", ".join(sorted(unknown))
            + "."
        )
    provider_id = _validate_identifier(provider["id"], label="provider id", limit=64)
    if PROVIDER_ID_RE.fullmatch(provider_id) is None:
        raise DesignerApiConfigError("provider id must use lower-case letters, digits, underscores, or hyphens.")
    if provider["auth_type"] not in ALLOWED_AUTH_TYPES:
        raise DesignerApiConfigError("auth_type must be bearer or x-api-key.")
    if provider["wire_api"] != WIRE_API:
        raise DesignerApiConfigError("wire_api must be exactly anthropic-messages.")
    max_tokens = provider["max_tokens"]
    if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_MAX_TOKENS:
        raise DesignerApiConfigError(
            f"max_tokens must be an integer from 1 through {MAX_MAX_TOKENS}."
        )
    api_key = _validate_api_key(provider["api_key"], allow_empty=True)
    normalized_provider = {
        "id": provider_id,
        "api_url": _validate_api_url(provider["api_url"]),
        "api_key": api_key,
        "model": _validate_identifier(provider["model"], label="model"),
        "auth_type": provider["auth_type"],
        "wire_api": provider["wire_api"],
        "max_tokens": max_tokens,
    }
    return {
        "schema": SCHEMA_VERSION,
        "role": ROLE,
        "provider": normalized_provider,
        "enabled": bool(api_key),
    }


def nonsecret_config(config: dict[str, Any]) -> dict[str, Any]:
    checked = validate_config(
        {
            "schema": config["schema"],
            "role": config["role"],
            "provider": dict(config["provider"]),
        }
    )
    provider = dict(checked["provider"])
    provider.pop("api_key")
    return {"schema": checked["schema"], "role": checked["role"], "provider": provider}


def config_sha256(config: dict[str, Any]) -> str:
    payload = json.dumps(nonsecret_config(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def endpoint_sha256(config: dict[str, Any]) -> str:
    return hashlib.sha256(config["provider"]["api_url"].encode("utf-8")).hexdigest()


def load_config(path: Path | str | None = None, *, codex_home_path: Path | str | None = None) -> dict[str, Any]:
    if path is not None and codex_home_path is not None:
        raise DesignerApiConfigError("Specify either path or codex_home_path, not both.")
    target = Path(path).expanduser() if path is not None else config_path(codex_home_path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        raise DesignerApiConfigError(
            f"Designer API configuration is missing at {target}. {_initializer_hint(target)}"
        ) from None
    except OSError as exc:
        raise DesignerApiConfigError(f"Could not inspect Designer API configuration at {target}.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DesignerApiConfigError(f"Designer API configuration is not a regular file: {target}.")
    if info.st_nlink != 1:
        raise DesignerApiConfigError(f"Designer API configuration has multiple hard links: {target}.")
    try:
        text = target.read_text(encoding="utf-8")
        data = json.loads(text, object_pairs_hook=_strict_object_pairs)
        return validate_config(data)
    except DesignerApiConfigError as exc:
        raise DesignerApiConfigError(f"Invalid Designer API configuration at {target}: {exc}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesignerApiConfigError(f"Designer API configuration at {target} is not valid UTF-8 JSON.") from exc


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=True, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary: Path | None = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise DesignerApiConfigError(f"Could not write Designer API configuration at {path}.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_credential(stdin: bool) -> str:
    try:
        value = sys.stdin.readline().rstrip("\r\n") if stdin else getpass.getpass("Designer API credential: ")
    except (EOFError, OSError, UnicodeError) as exc:
        raise DesignerApiConfigError("Could not read the Designer API credential.") from exc
    return _validate_api_key(value, allow_empty=False)


def _metadata(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    provider = config["provider"]
    return {
        "available": config["enabled"],
        "enabled": config["enabled"],
        "schema": config["schema"],
        "role": config["role"],
        "provider": provider["id"],
        "api_url": provider["api_url"],
        "model": provider["model"],
        "auth_type": provider["auth_type"],
        "wire_api": provider["wire_api"],
        "max_tokens": provider["max_tokens"],
        "path": str(path),
        "config_sha256": config_sha256(config),
        "model_call": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the direct-API Designer configuration.")
    parser.add_argument("--codex-home", type=Path, help="Override CODEX_HOME.")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER_ID)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--auth-type", choices=sorted(ALLOWED_AUTH_TYPES), default=DEFAULT_AUTH_TYPE)
    parser.add_argument("--wire-api", choices=[WIRE_API], default=WIRE_API)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--init-default", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"--api-key", "--credential"} or arg.startswith(("--api-key=", "--credential=")) for arg in raw):
        print("Error: API key must be supplied through getpass or --api-key-stdin.", file=sys.stderr)
        return 1
    try:
        args = parse_args(raw)
        path = config_path(args.codex_home)
        if args.status:
            print(json.dumps(_metadata(path, load_config(path)), sort_keys=True, separators=(",", ":")))
            return 0
        if (path.exists() or path.is_symlink()) and not args.force:
            raise DesignerApiConfigError(
                f"Designer API configuration already exists at {path}; use --force to replace it."
            )
        api_key = "" if args.init_default else _read_credential(args.api_key_stdin)
        data = {
            "schema": SCHEMA_VERSION,
            "role": ROLE,
            "provider": {
                "id": args.provider,
                "api_url": args.api_url,
                "api_key": api_key,
                "model": args.model,
                "auth_type": args.auth_type,
                "wire_api": args.wire_api,
                "max_tokens": args.max_tokens,
            },
        }
        config = validate_config(data)
        persisted = {"schema": config["schema"], "role": config["role"], "provider": config["provider"]}
        _atomic_write(path, persisted)
        verb = "Created disabled" if args.init_default else "Configured"
        print(f"{verb} direct-API Designer at {path}")
        return 0
    except DesignerApiConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
