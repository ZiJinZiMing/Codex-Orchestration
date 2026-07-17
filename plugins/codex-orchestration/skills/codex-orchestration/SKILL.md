---
name: codex-orchestration
description: Build multi-model Codex workflows by assigning compatible models to roles such as advisor, executor, researcher, reviewer, writer, or supervisor. Use when the user invokes Codex Orchestration to create custom roles, define a workflow, or set up, inspect, change, disable, or temporarily override model routing. Keep the selected task model as root and preserve Codex's planning, Goal, permissions, integration, and verification behavior.
---

# Codex Orchestration

The model selected when this Codex task started is already the orchestrator. Never ask the user to configure another one and never change the root model on this skill's behalf.

This skill adds a model route to Codex's existing multi-agent flow. It does not create another scheduler.

## Understand the command

Support these simple forms:

```text
/codex-orchestration setup executor: GPT-5.6 Luna Extra High
/codex-orchestration setup executor: GPT-5.6 Luna Extra High, advisor: Claude Fable 5 Extra High
/codex-orchestration create project role: researcher
/codex-orchestration create personal roles: researcher, writer, reviewer
/codex-orchestration status
/codex-orchestration disable
/codex-orchestration remove custom roles personally
/codex-orchestration executor: GPT-5.6 Terra high — <one task only>
```

`setup` installs or updates the personal one-time routing policy. `create project role` or `create personal role` creates native Codex custom-agent files. `status` inspects built-in routing. `disable` restores its pre-setup values.

`remove custom roles` cleans only verified plugin-managed advisor/executor files. Arbitrary native roles are user-owned. An invocation with seats and work but no control verb is a current-task override and must not rewrite config.

The executor is required for setup or a task-local override. It is not required for a custom-role creation request. The advisor is optional: if omitted, it means `advisor: none`. Do not ask a separate advisor question unless the user asks for help choosing one.

If the executor is missing, ask only:

```text
Which executor model and effort should Codex use? You can optionally include an advisor; omission means none.
```

Because explicit skills may not reload from a bare reply, include a ready-to-copy line using the exact label shown by the client and preserve the original work:

```text
<exact-skill-label> setup executor=<model>@<effort-or-auto>, advisor=<model>@<effort-or-auto>|none
```

For a task-local request, append `— <original task>`. Keep every supplied modifier. Do not lose the user's task while collecting a model choice.

If an old prompt contains `orchestrator:`, explain that the current task model already owns that role. Ignore that seat instead of switching or persisting it.

Normalize `Extra High` to `xhigh` for Codex models. `Claude Fable 5 Extra High` is the built-in advisor label; map it to `--advisor-fable --advisor-effort max`, not the Codex model catalog. Resolve every other display name to an exact ID only through the executing host's model catalog, picker, a loaded custom agent, or official provider documentation. Never invent an ID. For persistent direct routing, resolve `auto` to the catalog's concrete default.

Read [providers-and-models.md](references/providers-and-models.md) before setup, when clients disagree, when a model is absent, when providers differ, or when custom agents or legacy migration are involved.

## Create arbitrary custom roles

Use native Codex custom-agent files for roles beyond the built-in advisor and executor seats. Examples include researcher, reviewer, writer, supervisor, security auditor, browser debugger, or domain expert.

Use project scope when the user says `project`, `repo`, `workspace`, or `current project`. Write to `<trusted-project>/.codex/agents/<role-name>.toml`. Use personal scope only when explicitly requested and write to `~/.codex/agents/<role-name>.toml`.

Before writing:

1. Normalize the role name to lowercase snake case and validate `^[a-z][a-z0-9_]{0,62}$`.
2. Require a clear purpose and `developer_instructions` that keep the role bounded.
3. Resolve the model and effort from the active catalog or a user-confirmed exact ID.
4. If `model_provider` is supplied, require an existing configured and authenticated compatible provider. Never create provider access or collect credentials.
5. Use the current task permission mode by default. Add `sandbox_mode` only when the user requests it. A role may request a narrower sandbox; it never bypasses the parent task's authority.
6. Keep `agents.max_depth = 1` behavior unless the user explicitly asks for nested agents. A custom role should not create descendants by default.
7. Refuse symlinked paths, duplicate agent names, malformed TOML, and overwriting an existing file without explicit replacement approval.

A custom agent file must define `name`, `description`, and `developer_instructions`. It may also define `model`, `model_reasoning_effort`, `model_provider`, `sandbox_mode`, `mcp_servers`, and `skills.config` when supported.

Preview the path and complete TOML before writing. A literal create request authorizes a clean new file after preview. Replacing or deleting an existing user-owned role requires a separate explicit decision.

Do not add the plugin ownership marker to arbitrary roles. Do not claim `disable` or `remove custom roles` will remove them. Tell the user to start a new task after creation so Codex loads the new roles.

When the user supplies a sequence such as `researcher -> reviewer -> writer`, preserve it as task-level workflow instructions. The root orchestrator owns every handoff, resolves conflicting feedback, verifies the result, and may skip only optional steps.

If the user combines a workflow with a Codex Goal, leave Goal lifecycle and limits under Codex's normal Goal controls. The orchestration policy operates inside the Goal; this skill does not silently create, pause, resume, or clear it.

## One-time native setup

Use this path for a current same-provider setup such as Sol root to Luna or Terra executors. Claude Fable 5 is the one built-in cross-provider advisor exception because it runs through the bundled read-only MCP bridge using an explicit Claude Code or direct API transport.

1. Identify the Codex binary used by the active host. Do not assume the shell `codex` is the Desktop binary.
2. Resolve the exact executor and optional advisor IDs and efforts from that host.
3. Run the bundled native configurator from this skill's real directory with Python 3.11 or newer. Use `python3` on typical macOS/Linux hosts; on Windows select an available `py -3.11` or `python` launcher after checking its version. Never use a repository-relative copy from the user's workspace.
4. Inspect the dry-run output. A literal `setup` request authorizes applying a clean, non-replacement personal policy after that preview.
5. Start a new task after apply. The user chooses the orchestrator in the normal model picker and no longer needs to invoke this skill for ordinary work.

Typical dry run and apply:

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --executor-model gpt-5.6-luna \
  --executor-effort xhigh

python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --executor-model gpt-5.6-luna \
  --executor-effort xhigh \
  --apply
```

Add `--advisor-model` and `--advisor-effort` for a same-provider Codex advisor. For Claude Fable 5, use `--advisor-fable --advisor-effort max --advisor-auth-mode auto --advisor-transport claude-code`. Present its route as exactly one of three advisor paths: **Claude Code CLI** (`claude-code`), **CCSwitch** (`direct-api` plus `user-settings`), or **Python API** (`direct-api` plus `config-file`; explicit legacy `environment` state is also reported as Python API). The saved source is authoritative and paths never fall back to each other. The authentication mode is `subscription`, `api`, or `auto`; `direct-api` requires explicit `--advisor-auth-mode api --advisor-api-source config-file|environment|user-settings`. `auto` checks subscription only when no API credential is configured; when API configuration exists it fails closed and requires explicit API mode and source, preventing an accidental metered route. The configurator chooses an available Python 3.11+ MCP launcher and performs only a local auth/capability check during setup. It makes no model call during setup or status. Omission persists `advisor: none`.

For the Python API path, first run `python3 <skill-dir>/scripts/configure_fable_api.py --init-default` in the user's terminal. It creates, without overwriting, a schema-2 provider file at `CODEX_HOME/.codex-orchestration-fable-api.json` containing the default OpenRouter Messages URL, an empty `api_key`, provider model `anthropic/claude-fable-5`, and bearer authentication. URL, key, model, and auth type are one provider configuration. The provider model may be another safe provider identifier, while the orchestration identity remains Claude Fable 5. An empty key disables Python API before any network request. To enable the disabled default through a hidden key prompt, rerun without `--init-default`, add `--force`, and supply the non-secret URL, model, and auth type as options; never ask the user to paste a key into chat or place it on the command line. Schema-1 files remain strictly readable and are never rewritten automatically. Routing state stores only non-secret path/transport/authentication/source enums. Do not delete or rewrite the provider file during routing disable.

The configurator capability-tests the complete v2 control preset on the active target, `codex` on PATH when different, the known macOS Desktop binary when present, and every explicit `--compat-bin`. A successful isolated config probe means that client can parse the preset; it is not a live child-model confirmation. Report `route accepted` or `used and confirmed` only from the exact live spawn evidence defined below. Ask about other Codex/IDE installations that share this config only when the environment suggests they exist, and pass their binaries explicitly. If the request or active host indicates a named `--profile`, explain that normal setup manages the default user layer and is not verified for that profile; do not add a routine question for users with no profile signal. If a checked client rejects any managed field, stop before apply. Recommend updating it or using the task-local fallback. `--allow-incompatible-client` requires a separate explicit user decision because it can make the shared config unreadable to that client.

For the current validated v2 direct route, set `tool_namespace = "agents"`. Live testing on Desktop `0.144.0-alpha.4` showed that the default reserved `collaboration.spawn_agent` schema rejected expanded model/effort metadata, while `agents` accepted the same request and spawned Luna at `xhigh`. Treat this as a required control-surface setting for that tested path, not as the executor selection. `usage_hint_text` carries the actual executor/advisor route.

The configurator explicitly enables multi-agent v2. On Codex 0.144.4, `agents.max_threads` cannot coexist with v2; setup moves that legacy child limit to `max_concurrent_threads_per_session` and adds one slot for the root, while disable restores the original fields. It intentionally manages:

- `features.multi_agent_v2.enabled`;
- `features.multi_agent_v2.hide_spawn_agent_metadata`;
- `features.multi_agent_v2.tool_namespace`;
- `features.multi_agent_v2.multi_agent_mode_hint_text`;
- `features.multi_agent_v2.usage_hint_text`.

When Claude Fable 5 is selected, it additionally manages only the plugin-scoped `enabled` override for the chosen bundled MCP launcher and any launcher variant already overridden by the user. All bundled variants are disabled by default. The original override values are stored and restored by `disable`. Codex's TOML editor may retain an inert empty table header after deleting the last override; never rewrite the file merely to remove that cosmetic header.

It uses Codex App Server's `config/read` and `config/batchWrite` APIs, not a home-grown TOML rewrite. It preserves unrelated settings and comments, validates the whole effective config, and uses the user-layer version to detect races. Restore snapshots cover the v2 controls, any migrated thread limit, and the narrowly scoped MCP overrides only when Fable is selected; the namespaced state also records schema/version markers, config path, selected seats, Fable's non-secret transport/authentication/source enums, and scalar-conversion metadata when needed. It never stores a credential, endpoint, account identifier, or plan metadata. If the user explicitly replaces existing hint text, the exact prior text is stored for restoration; warn them never to place credentials in routing hints.

If a user-authored mode or usage hint already exists, do not replace it automatically. Show the conflict. Use `--replace-existing-policy` only after the user explicitly approves replacing and later restoring those exact values.

## Status, change, and disable

For status:

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --status

python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --status --require-effective
```

Run status from the target project. The first form is descriptive. Use `--require-effective` for automation and release gates; it returns nonzero for incompatible clients, conflicts, overrides, incomplete controls, unavailable advisor or agent routes, or orphaned v0.4+ personal roles. Report the current task model as the orchestrator, the configured executor and advisor, the selected advisor path (`Claude Code CLI`, `CCSwitch`, or `Python API`), Fable's transport, authentication mode and source, whether v2 and the personal policy are effective in that workspace, whether spawn controls are visible, whether the effective tool namespace is `agents`, the target config path, and checked-client compatibility. State that neither status form proves a live child route; only fresh child-session metadata does.

To change seats, run normal `setup` again. The configurator keeps the original restore snapshot rather than treating its own managed values as user settings.

For disable, dry-run and then apply. A literal `disable` request authorizes a clean restore:

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --disable

python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --disable --apply
```

Disable must remain available even if an older client is incompatible with the active policy. Refuse to erase managed fields that the user edited after setup; explain the conflict instead.

For personal v0.4 custom roles, preview and apply removal with `configure_orchestration.py --scope personal --personal-route-names --remove-saved-roles`. For older fixed-name personal roles, run a separate preview without `--personal-route-names`. Project removal uses `--scope project --root <trusted-project> --remove-saved-roles`. Delete only files that the configurator fully validates as managed; edited or user-owned files require manual review.

## Claude Fable 5 advisor

Use this built-in route when the user names Claude Fable 5. Do not create a custom provider or custom-agent file for it.

In every user-facing status or result, use the exact name `Claude Fable 5`. Report the selected advisor path as `Claude Code CLI`, `CCSwitch`, or `Python API`, plus the transport and active authentication path. Do not expose or restate credentials, endpoints, or account identifiers, and do not expose or restate Claude account-plan metadata.

Prerequisites:

- `claude-code` transport has the official `claude` CLI installed;
- `subscription` mode has a first-party Pro or Max account login and no configured API credential;
- `api` mode either has a validated standalone config file or has `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or, for `claude-code` only, `apiKeyHelper` configured in the explicitly saved `environment` or `user-settings` source;
- a Python 3.11+ launcher is available.

The plugin packages three disabled MCP launcher variants for macOS, Linux, and Windows. Setup enables exactly the compatible Python variant through the plugin's namespaced config. The bridge excludes Bedrock/Vertex/Foundry in every mode. `subscription` removes API keys, auth tokens, Base URL, custom headers, and user/project/local settings, and fails closed if an API credential is configured. `api` accepts only the explicitly saved `config-file`, `environment`, or `user-settings` source, removes injected Claude OAuth overrides, and does not require subscription metadata. `config-file` is valid only with `direct-api`; it reads only `CODEX_HOME/.codex-orchestration-fable-api.json` and never reads API settings from the process environment, `~/.claude/settings.json`, or CC Switch. `auto` uses subscription when no API credential source exists and otherwise requires explicit API mode and source.

`claude-code` invokes `claude -p --model claude-fable-5` with a fixed session name, `--safe-mode`, no tools, no session persistence, prompt suggestions and automatic session-title traffic disabled, every built-in default-model slot pinned to Fable, no `--fallback-model`, and JSON output. A `user-settings` API route extracts only API credential/transport values or the `apiKeyHelper` path, disables setting sources, and adds `--bare`.

`direct-api` requires one static token or API key and rejects `apiKeyHelper`, custom headers, ambiguous credentials, unsafe URLs, redirects, and every subscription/auto combination. It sends exactly one dependency-free Python request with zero retries to the selected Anthropic-compatible Messages endpoint, uses a 131,072-token output cap and a 600-second request timeout, requires `anthropic-version: 2023-06-01`, and accepts only `stop_reason=end_turn`. A refusal remains unavailable and fail-closed, but its provider-supplied refusal type, category, and explanation are returned through a bounded secret-safe diagnostic that never includes response content. CCSwitch sends the canonical Fable request through the saved user-settings proxy. Python API sends its configured provider model and requires the response to echo that exact provider model before normalizing the result to canonical `model = claude-fable-5` and `used_models = [claude-fable-5]`; a mismatch fails before canonical advisor metadata is returned. The provider file is reloaded and validated for every review; a blank key, deletion, corruption, permission failure, or invalid field makes Python API unavailable without source or path fallback. The saved effort is reported as `configured_effort` but is not sent or applied. The saved route pins the canonical model and non-secret path/transport/authentication/source enums; the root cannot replace them through tool arguments.

The bridge accepts only one self-contained `packet`. It requests `PLAN_APPROVED` or `PLAN_REVISE` as the first non-empty line. Claude Code requires runtime `modelUsage`, after deduplication, to be exactly `["claude-fable-5"]`; direct API requires the sole response model to echo exactly the same ID and reports that one locally requested model. Missing or additional model metadata fails before a decision can be returned. If an exclusively confirmed Fable response omits the marker, the bridge conservatively returns `PLAN_REVISE`, never approval. Any auth, transport, redirect, truncated/empty response, or model-confirmation failure is `advisor unavailable`. It returns no account identifier or credential. Direct verification proves one local request and an exact echoed ID; it cannot prove that an upstream gateway did not remap internally.

## Durable or cross-provider custom agents

Direct `model` routing is same-provider. Except for the built-in Claude Fable 5 MCP route above, a different provider needs an already authenticated Codex-compatible provider and a loaded custom agent that pins `model_provider`.

Use the existing standalone-agent configurator for this extended path. Personal scope is required for machine-local provider IDs and affects all projects, so the user's explicit cross-provider `setup` request must name or confirm the existing provider ID. Never create provider definitions, collect keys in chat, or write credentials.

First preview and apply the namespaced custom agents:

```bash
python3 <skill-dir>/scripts/configure_orchestration.py \
  --scope personal \
  --personal-route-names \
  --codex-bin <active-codex-binary> \
  --executor-model <exact-id> \
  --executor-effort <effort> \
  --executor-provider <existing-provider-id> \
  --advisor-model <exact-id> \
  --advisor-effort <effort> \
  --advisor-provider <existing-provider-id>
```

When this cross-provider/custom-agent setup omits an advisor, pass `--remove-advisor` so a previously managed advisor is not left as a misleading saved seat. Apply only after a clean preview. Then point the native policy at the loaded role names:

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --executor-agent <reported-executor-agent-name> \
  --advisor-agent <reported-advisor-agent-name> \
  --apply
```

Omit `--advisor-agent` when none is configured. `--personal-route-names` generates stable CODEX_HOME-specific names and prints them for the native command. The native configurator verifies exactly one matching personal file and refuses a same-name project role in the current workspace. A custom-agent file is a stronger durable model/provider pin than a direct tool hint, but runtime identity is confirmed only when the host exposes it. Start a new task so Codex loads the role files.

These are two separate storage transactions. If the native command fails after the role transaction applied, immediately preview and then apply:

```bash
python3 <skill-dir>/scripts/configure_orchestration.py \
  --scope personal \
  --personal-route-names \
  --codex-bin <active-codex-binary> \
  --remove-saved-roles
```

Remove only files the configurator validates as managed. If cleanup fails or the operation was interrupted, stop and run native `--status --require-effective`; report each orphaned managed role for manual review. Never claim the two stores changed atomically. On Windows, new managed roles can be created, but updating or removing an existing role fails closed; explain that limitation before choosing the custom-agent path.

The standalone configurator also retains project-scoped saved roles, safe removal, and opt-in migration for releases 0.1–0.3. It must never change the root model, permissions, credentials, or global agent limits.

## Preserve Codex's decisions

The current task model remains the root. It owns intent, planning, architecture, decomposition, delegation, integration, review, final verification, and the final answer.

Codex decides whether a plan helps, whether any work is safely delegable, how many independent slices exist, and whether parallelism is worth its context and integration cost. Keep simple, tightly coupled, context-heavy, and root-owned work with the root.

This skill and its saved policy must never:

- create a second orchestrator;
- force a spawn or fixed worker count;
- create or change Goal state;
- weaken approvals or permissions;
- create nested executor teams;
- let an advisor direct executors;
- parallelize overlapping writes;
- silently substitute the root model for an unavailable child route.

An explicit `no subagents` instruction always wins. A current-task seat override wins over the saved default for that task only.

## Spawn routed children correctly

Inspect the callable subagent interface. A saved current preset should expose the routed tool under `agents`; if only `collaboration` is exposed, do not assume the expanded direct route works. For a task-local fallback, use whichever callable namespace is actually present and pass exact route controls only when its schema exposes them.

Every spawn that supplies `model`, `reasoning_effort`, or `agent_type` through this skill must use:

```text
fork_turns = "none"
```

A small positive partial fork is technically valid in Codex, but this skill deliberately requires `none`: it minimizes duplicated context and makes the root send a deliberate self-contained packet. Never use the default `all` with a different route. Full-history forks inherit the root model and Codex rejects the override.

For a direct executor route, pass the exact configured model and concrete effort. For a custom route, pass the exact namespaced `agent_type`. Do not force a service tier; supported children may inherit Fast/priority from the parent, so tell users who prioritize allowance savings not to run the root in Fast mode.

Direct model overrides keep the root's provider. Before a direct spawn, establish that the target model is on the same provider. If it differs or cannot be established, mark the route unavailable and require a custom agent that pins `model_provider`.

After spawning, use the tool result or client metadata to confirm the accepted route. Distinguish:

- `native policy installed`: the managed user policy exists; activation still depends on effective workspace config;
- `pinned custom agent available`: a matching role is loaded, but has not run;
- `route accepted`: the current tool accepted and validated the requested route controls;
- `used and confirmed`: use only when the client explicitly exposes effective runtime model/provider/effort metadata;
- `inherited root — requested child model was not used`;
- `unavailable`: the requested route cannot run here;
- `none`: no advisor is configured.

Tool acceptance proves the requested route was valid and accepted, not necessarily that the client exposes post-start runtime identity. Child prose claiming a model name is not proof. If an exact route fails, report it to the root. Continue root-owned work only when the user did not make delegation or that seat a hard requirement.

## Advisor review

Use an advisor only when configured and the root has a non-trivial plan or executor slices worth reviewing. Skip it for simple work.

Before executor work, send one advisor a self-contained packet containing:

- user intent and acceptance criteria;
- relevant repository facts and constraints;
- the root's plan and proposed executor slices;
- dependencies, ownership, and sequencing;
- material risks and verification checks.

Tell the advisor to review only, report only to the root, avoid edits and mutation, never spawn, and never contact executors. Require exactly one first-line signal:

```text
PLAN_APPROVED
PLAN_REVISE
```

`PLAN_APPROVED` means no material gap was found in the supplied packet, not that success is guaranteed. `PLAN_REVISE` must give prioritized material gaps and a concrete correction for each. Style preferences do not justify revision.

The root adjudicates every suggestion and owns the revised plan. Allow at most one confirmation pass after a material revision. A configured advisor is a gate for a non-trivial executor plan by default. The response must report `model = "claude-fable-5"` and `used_models = ["claude-fable-5"]`; missing or mixed model metadata, transport failure, malformed output, inaccessible routing, or missing context means `advisor unavailable`, never approval. Stop before executor work unless the user explicitly made the advisor best-effort.

For Claude Fable 5, call the configured MCP server's `review_plan` tool instead of spawning an advisor child. It remains root-only and read-only; executors never receive the tool or direct it.

## Executor handoff

Give each executor one bounded packet with:

- objective and boundaries;
- only the context and repository facts it needs;
- owned files or explicit read-only scope;
- dependencies and stop conditions;
- acceptance criteria and smallest useful verification;
- required handoff format.

Require it to preserve unrelated work, stay inside the slice, avoid the advisor, avoid descendants, and report blockers rather than guess. The handoff includes status, work completed, files or evidence, checks run, and remaining risks.

Parallelize only genuinely independent slices with non-overlapping write ownership. The root inspects, integrates, and verifies every handoff. Executor completion is never final acceptance.

## Task-local and older-client fallback

When the persistent policy is unavailable, apply the supplied seats only to the work in the same invocation. Do not claim that a mutable team was saved.

Use the strongest exact control the current client exposes:

1. a matching loaded namespaced custom agent;
2. accepted direct `model` and `reasoning_effort` inputs with `fork_turns = "none"`;
3. a clearly labeled prompt preference when exact routing is unavailable;
4. `unavailable` when the provider or model cannot be reached.

For task-local `auto`, omit the reasoning-effort input. Never pass the literal string `auto` to a spawn tool; the effective inherited or host-chosen effort remains unverified unless the client exposes it.

Report a compact activation status and continue the included task:

```text
Codex Orchestration
Orchestrator: <active model or current task model> — active
Executor: <model>@<effort> — <route state>
Advisor: <model>@<effort> — <route state>, or none
Delegation: Codex decides when it helps; Plan and Goal behavior unchanged
```

Never report a prompt preference or saved file as a model that actually ran. Report an exact tool call as `route accepted`; reserve runtime confirmation for explicit effective metadata.

## Keep savings language honest

The purpose is to spend high-end capacity where judgment matters and use an efficient coding model for eligible execution volume. Do not create agents solely to hit a percentage.

The “about 65%” example is a model-weighted credit calculation: at the published Luna rate of 20% of Sol, a comparable token mix with 20% on Sol and 80% on Luna costs `0.20 + (0.80 × 0.20) = 0.36`, about 64% fewer credits before orchestration overhead.

Never call that 65% fewer raw tokens, a guaranteed five-hour or weekly-limit saving, a fixed monetary saving, or five times more completed work. Advisor calls, duplicated context, retries, tools, Fast service tier, and unnecessary workers can reduce or erase the benefit.

## Resources

- `scripts/configure_native_routing.py`: one-time native setup, status, update, and disable.
- `scripts/fable_advisor_mcp.py`: fail-closed Claude Fable 5 plan-review bridge.
- `scripts/configure_orchestration.py`: namespaced custom agents, provider pins, safe removal, and legacy migration.
- `scripts/inspect_models.py`: fallible host-catalog diagnostics.
- [providers-and-models.md](references/providers-and-models.md): detailed capability, provider, compatibility, persistence, and usage boundaries.
