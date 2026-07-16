# Security policy

## Supported versions

Security fixes are made on the latest released version. Upgrade before reporting a problem that is already fixed on `main`.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/Cjbuilds/Codex-Orchestration/security/advisories/new) and include:

- the affected version and Codex client version;
- operating system and installation scope;
- a minimal reproduction;
- the security impact and any known workaround.

Do not include credentials, tokens, or private configuration. You should receive an acknowledgement within seven days. A coordinated disclosure date will be agreed after the impact and fix are verified.

## Security boundaries

Codex-Orchestration writes only its documented Codex routing fields, managed custom-agent files, and the optional standalone Fable API config explicitly initialized by the user. Environment and Claude-settings routes do not copy or persist credentials. The standalone initializer securely prompts for one credential and stores it in `CODEX_HOME/.codex-orchestration-fable-api.json`; it never accepts the credential as a command-line argument or emits it in status, logs, routing state, or tool results. The write is atomic and requests owner-only permissions where supported, but local administrators can still read a user-owned file and Python's Windows mode bits do not prove Unix-equivalent ACL isolation. Protect and rotate this metered credential, never commit or share the file, and delete it explicitly when no longer needed.

The optional direct Fable transport reads only the explicitly selected source into process memory long enough to send one fail-closed request. The saved source is authoritative: Python API `config-file` mode never reads API credentials, URLs, or models from environment variables, Claude settings, or CCSwitch, even when its configured key is blank. A blank key disables Python API before request construction. The provider response must echo the configured provider model before the bridge returns canonical Fable metadata. Codex-Orchestration does not weaken sandbox or approval settings or guarantee that policy-guided routing is engine-enforced. See the README for the exact runtime-verification boundary.
