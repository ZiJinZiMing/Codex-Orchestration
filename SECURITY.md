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

Codex-Orchestration writes only its documented Codex routing fields and managed custom-agent files. It does not create providers, solicit or persist provider credentials, log selected API credentials, weaken sandbox or approval settings, or guarantee that policy-guided routing is engine-enforced. The optional direct Fable transport reads one explicitly selected credential into process memory only long enough to send one fail-closed request. See the README for the exact runtime-verification boundary.
