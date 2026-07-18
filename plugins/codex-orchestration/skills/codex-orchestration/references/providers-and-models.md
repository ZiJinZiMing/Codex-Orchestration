# Models, Providers, and Routing Boundaries

Use this reference for setup, client compatibility, custom providers, or a route that does not behave as requested.

## The shortest correct model

1. The model selected for the Codex task is the root orchestrator.
2. A current Sol or Terra root uses multi-agent v2.
3. The saved policy tells the root which exact route to request for an optional Planner, optional Advisor, and delegated Executors.
4. Codex still decides whether a spawn helps.
5. Every different-model, different-effort, or custom-agent child uses `fork_turns = "none"` and a self-contained packet.
6. The root reviews and verifies all child work.

The policy tells Codex which routes to request for configured planning, review, and delegated execution. It does not force every task to delegate or add a second scheduler.

## Current capability matrix

These facts were source-checked and runtime-tested on July 10, 2026. Always capability-test the actual host because the fields are still evolving.

| Capability | Current behavior | Consequence |
| --- | --- | --- |
| Sol/Terra catalog metadata | May advertise v2 depending on the active catalog/provider | Setup does not rely on this metadata; it explicitly enables v2. |
| Luna model metadata | `multi_agent_version = v1` in the tested catalog | Luna still works as a v2 child; the parent session owns the v2 spawn surface. |
| `hide_spawn_agent_metadata = false` | Shows `agent_type`, `model`, `reasoning_effort`, and `service_tier` on v2 spawn | Required for direct route control; it does not select a route alone. |
| `tool_namespace = "agents"` | On live-tested Desktop `0.144.0-alpha.4`, the default `collaboration` namespace rejected expanded model/effort metadata; `agents` accepted it and spawned Luna at `xhigh`. | Required for this validated direct-routing path. It changes the callable namespace but does not select Luna. |
| `usage_hint_text` | Appended to the spawn tool description | Carries the exact Planner/Advisor/Executor routes where the root chooses children. |
| `multi_agent_mode_hint_text` | Replaces the default proactive/explicit mode hint and is sent to root and child tasks | Must contain both root and child boundaries. |
| Claude Fable 5 MCP route | Root-directed `create_plan` and `revise_plan` use the authenticated `claude-code` transport; `review_plan` uses the saved `claude-code` or `direct-api` transport and explicit authentication mode | Built-in cross-provider Planner or Advisor exception; current MCP requests do not provide caller identity, so caller isolation is policy-enforced. |
| `fork_turns` default | `all` | Different model/effort/role overrides are rejected unless the call uses `none` or a positive partial fork. |
| Effective concurrency | v2 uses `max_concurrent_threads_per_session`, including the root | Setup converts legacy `agents.max_threads = N` to `N + 1` and restores it on disable. |
| Older CLI 0.142.5 | Rejects `multi_agent_mode_hint_text` as an unknown feature-table field | Never write the global native policy without checking every known shared-config client. |

The installer does not infer support from version strings. It launches each detected binary with an isolated `CODEX_HOME` and probes whether it can parse the routing-control fields. That is a config-compatibility check, not proof of a live child route.

## Why setup explicitly enables v2

Codex 0.144.4 can expose `wait_agent` without exposing a usable v1 spawn namespace under some provider/capability combinations. The routing policy therefore sets `features.multi_agent_v2.enabled = true` instead of relying on model-catalog metadata. Because that release rejects `agents.max_threads` while v2 is enabled, setup migrates it to the v2 session limit and preserves restoration data.

Enabling v2 can:

- show an under-development feature warning;
- change behavior for unrelated root models without the user asking.

Setup resolves the Codex 0.144.4 `agents.max_threads` conflict through the reversible migration above.

If the user's config uses the older scalar form `multi_agent_v2 = true|false`, the configurator temporarily converts that value to the equivalent table form and records the original scalar. Disable restores the exact boolean only if no other table fields were added afterward.

## What the managed v2 fields do

The control surface and the route are separate:

- `enabled = true` activates the v2 tool surface;
- `hide_spawn_agent_metadata = false` exposes the model, effort, agent-type, and service-tier spawn inputs;
- `tool_namespace = "agents"` makes the expanded route callable on the currently validated Desktop build;
- `multi_agent_mode_hint_text` carries the root/child behavior and safety boundaries;
- `usage_hint_text` carries the exact optional Planner, optional Advisor, and required Executor routes.

`multi_agent_mode_hint_text` describes the policy:

- current task model is the one root orchestrator;
- Codex decides whether delegation is useful;
- optional Planner drafts and revises through the root; omitted Planner means the root plans;
- optional Advisor is directed through the root and reviews through a five-round bounded approval loop before Executor work;
- executor packets are bounded and self-contained;
- children do not create descendants;
- user overrides and `no subagents` win;
- Goal, permissions, approvals, and worker counts are not changed.

`usage_hint_text` attaches the route to the spawn tool itself:

```text
planner  -> model="gpt-5.6-sol", reasoning_effort="high", fork_turns="none"
advisor  -> model="gpt-5.6-terra", reasoning_effort="high", fork_turns="none"
executor -> model="gpt-5.6-luna", reasoning_effort="xhigh", fork_turns="none"
```

For a durable custom-agent route it uses:

```text
agent_type="codex_orchestration_executor", fork_turns="none"
agent_type="codex_orchestration_advisor", fork_turns="none"
```

For Claude Fable 5 it names the enabled bundled MCP server and tells the root to use `create_plan`/`revise_plan` for the Planner seat or `review_plan` for the Advisor seat. These are root tool calls, not `spawn_agent`, so `fork_turns` does not apply.

The custom mode text is visible in spawned children too. That is why it says: if root, orchestrate; if child, stay within the packet and never spawn.

## Routing strength and its honest boundary

There is no global Codex field named `executor_model`. The native same-provider route combines:

- visible v2 spawn metadata under the validated `agents` namespace;
- persistent spawn-tool guidance;
- a model-visible exact `model` and `reasoning_effort` input;
- runtime catalog validation when the tool call is accepted;
- optional effective-runtime confirmation when the client exposes it.

That is strong routing, but it is not a separate engine-level scheduler. The root can still choose not to delegate. Tool acceptance proves Codex accepted and validated the requested route; it does not guarantee that every client exposes the effective post-start identity. If the model ignores the required route or the tool rejects it, report that mismatch rather than claiming success.

Setup runs before a future task chooses its root, so it cannot persist a mechanically verified future root-provider identity. Direct routes are valid only when the active task can establish that the requested model belongs to the inherited root provider. If provider identity is missing or ambiguous, fail closed and use a provider-pinned custom agent.

A custom-agent file is the stronger persistent pin for a reusable role because the role config can set `model`, `model_reasoning_effort`, and `model_provider`. A stronger live parent override can still win, so confirm the effective child metadata either way.

## Forking rules

V2 `spawn_agent` defaults to a full-history fork. Full-history children inherit the root model, provider, and reasoning effort. Codex therefore rejects `agent_type`, `model`, or `reasoning_effort` on a fork with `fork_turns = "all"`.

Use:

```text
fork_turns = "none"
```

and send a self-contained task packet. A small positive turn count also permits overrides, but `none` is the Codex-Orchestration default because it minimizes duplicate context and makes the handoff deliberate.

Correctness wins over context savings. If a bounded packet cannot carry the necessary context safely, keep the work with the root instead of forcing a cheaper child.

## Start with the executing host

Do not keep a static display-name alias table. Model IDs, efforts, access, providers, and model metadata change.

Resolve seats in this order:

1. active host's App Server `model/list` result;
2. current client model picker or accepted spawn controls;
3. a loaded namespaced custom agent;
4. exact binary catalog diagnostics;
5. official provider documentation;
6. user-supplied exact ID when the sources are ambiguous.

`scripts/inspect_models.py` and debug catalog commands are useful signals, not permanent APIs. A missing shell-CLI model does not prove a newer Desktop model is unavailable. Always report which binary and catalog supplied the model IDs for a persistent preset; do not call that a live route confirmation.

For task-local `auto`, omit the effort override and call the effective effort unverified until exposed. For persistent direct or custom-agent routing, resolve `auto` to the model's concrete catalog default so the root effort cannot leak into the child.

## Native persistence and restoration

`configure_native_routing.py` writes the personal user config because the policy is meant to work in later tasks and projects.

It uses the official App Server flow:

```text
initialize -> initialized -> config/read(includeLayers=true)
           -> config/batchWrite(expectedVersion=...)
           -> config/read verification
```

The App Server permits writes only to the user config. It performs full schema and managed-requirement validation, preserves TOML comments and unrelated fields through `toml_edit`, atomically persists the file, returns `okOverridden` when a higher layer wins, and rejects a stale user-layer version.

The configurator writes each owned nested field separately, except when converting a legacy boolean feature shape. It refuses to replace user-authored hint strings unless `--replace-existing-policy` was explicitly approved. Setup verifies both the user layer and the effective config in the current workspace; it rolls back when a project or managed layer already overrides the installed policy there.

Restore state lives at:

```text
~/.codex/.codex-orchestration-routing.json
```

It contains the prior and managed values of the v2 controls, any migrated thread limit, chosen Planner/Advisor/Executor routes, schema/version markers, scalar-conversion metadata when needed, and config path. When Claude Fable 5 is selected, it also records the non-secret path/transport/authentication/source enums and only the plugin-scoped MCP launcher overrides that setup touched. It never copies provider definitions, endpoints, auth stores, account identifiers, plan metadata, or credentials. A normal clean setup contains generated policy text, the namespace value, seat IDs, and restoration metadata. Explicit replacement must retain the user's exact old hint text so disable can restore it; routing hints must never contain credentials. State is written with a same-directory atomic replacement and restrictive file mode where supported. If persistence fails after config apply, the configurator rolls the config back using the returned version.

Disable compares every current managed value before restoration. If the user edited a managed field after setup, it stops instead of erasing that work. Without state, each surviving marker proves ownership only of that hint string. Disable may safely remove the marked string or strings, but it leaves metadata visibility and the tool namespace unchanged because their previous values are unknown.

## Shared-config compatibility

Desktop and CLI commonly share `~/.codex/config.toml`. A field supported by Desktop can prevent an older CLI from starting at all.

Setup automatically checks the installations it can identify:

- the supplied active-host binary;
- `codex` on PATH when different;
- the macOS Desktop embedded binary when present;
- every explicit `--compat-bin`.

Ask the user about alternate Desktop, IDE, container, or Windows installations that share the same home, because no open-source installer can discover every possible binary path. Pass each known path with `--compat-bin`.

If any checked binary rejects the complete preset, normal setup fails before writing. Preferred resolution: update that client. The per-task skill workflow remains available without a global policy. Successful parsing does not prove that a future task selected a v2 root or that a live model route was accepted.

`--allow-incompatible-client` is an escape hatch only after the user explicitly accepts that the named client may stop loading the shared config. Disable never blocks on this compatibility check; otherwise the policy could trap the user.

## Custom agents

Codex's reusable role format is one TOML file per custom agent:

```text
<project>/.codex/agents/*.toml
~/.codex/agents/*.toml
```

Project-scoped/legacy saves use these fixed names:

```text
codex-orchestration-executor.toml -> codex_orchestration_executor
codex-orchestration-advisor.toml  -> codex_orchestration_advisor
```

Personal roles used by the global native policy add a stable 12-character suffix derived from the canonical `CODEX_HOME` path:

```text
codex-orchestration-executor-<personal-id>.toml -> codex_orchestration_executor_<personal-id>
codex-orchestration-advisor-<personal-id>.toml  -> codex_orchestration_advisor_<personal-id>
```

This prevents accidental shadowing by the older fixed project names. The native configurator requires exactly one matching personal role and refuses a same-name project role in the current workspace. Because project roles have higher precedence, run status in each project before relying on a personal custom-agent route; a deliberately duplicated suffixed name can still shadow it.

The executor file says to implement only the root's bounded packet, preserve unrelated work, verify, report, and never spawn. The advisor file says to review only the root's packet, request a read-only sandbox, return `PLAN_APPROVED` or `PLAN_REVISE`, and never edit, delegate, or contact executors.

Custom agents load in a new task. Writing a file does not hot-load it into an existing task. A project-scoped role loads only from a trusted project. If the same role name exists in project and personal scope, report the collision instead of guessing precedence.

Treat a saved scope as one complete team anchored by its executor. A missing same-scope advisor means `advisor: none`; never silently borrow an advisor from another scope.

The standalone-agent configurator remains dry-run first, rejects symlinks and hard links, preserves supported metadata, journals multi-file transactions without storing config contents, refuses edited or user-owned files, and uses opt-in backup-first migration for known output from versions 0.1–0.3.

The role-file transaction and native App Server policy transaction are independent. After a phase-two failure, remove only fully validated newly managed roles. Native status reports collision-resistant managed personal roles that are not referenced by its current restore state, and `--require-effective` treats them as unhealthy. This recovery is compensating cleanup, not atomicity across the two stores.

On Windows, the custom-agent configurator can create a new role but refuses in-place update or removal of an existing managed role because it cannot prove the Unix inode/metadata-preservation contract. Native App Server policy setup and disable are separate and remain capability-tested through the active Codex binary.

## Provider boundaries

Direct v2 `model` overrides retain the parent's provider. They are the simplest route for an OpenAI root and OpenAI Luna/Terra child.

Claude Fable 5 is the explicit built-in Planner or Advisor exception. The plugin does not pretend it is a Codex model or translate Anthropic into the Responses protocol. Instead, a disabled-by-default local MCP server uses one explicit transport: the official `claude` CLI or, for Advisor review, a direct Anthropic-compatible Messages request. Setup enables one Python 3.11+ launcher variant, and disable restores every prior plugin override value. Missing transport in legacy state means `claude-code`. Codex's TOML editor can retain an inert empty table header after its final key is deleted; the configurator does not risk a broad TOML rewrite for cosmetic cleanup.

`subscription` requires a first-party Pro or Max login and the `claude-code` transport; it removes API credentials, Base URL, custom headers, and provider overrides, and refuses to run while supported API configuration exists. User-facing Fable routes are classified as Claude Code CLI (`claude-code`), CCSwitch (`direct-api` plus `user-settings`), or Python API (`direct-api` plus `config-file`; explicit legacy `environment` state remains compatible). The saved source is authoritative and never falls back. Python API reads only `CODEX_HOME/.codex-orchestration-fable-api.json`: schema 2 groups URL, API key, provider model, and auth type; the default key is empty and disables the path, while the default provider model is `anthropic/claude-fable-5`. Strict schema-1 files remain readable without rewrite. `auto` checks subscription only when no API configuration exists and otherwise requires explicit API mode/source.

The `claude-code` transport accepts a static credential or `apiKeyHelper`, isolates user settings, adds `--bare` for API auth, pins every Claude Code model slot and applied effort, disables tools/session persistence/title traffic/refusal fallback, and validates the exact transport-specific runtime model set. The subscription path permits only its explicitly documented helper model in addition to Fable; API-backed paths require canonical Fable-only metadata. The `direct-api` transport accepts exactly one static credential, rejects helpers/custom headers/ambiguous sources/unsafe URLs, follows no redirects, performs zero retries, sends no effort field, and requires `stop_reason=end_turn`. Its standalone initializer stores the exact Messages URL, provider model, authentication type, and API key in one user-owned file with atomic write and best-effort restrictive permissions; the key is never accepted as an argument or emitted by status. Runtime reloads the selected source on every request and fails closed if it is missing, disabled, or invalid. CCSwitch and explicit legacy environment routes accept only the canonical or Anthropic-qualified Fable model echo. Python API instead sends its configured provider model, requires the response model to match that provider field byte-for-byte, preserves both values as `request_model`/`response_model`, and then canonicalizes the advisor identity to Fable-only `model`/`used_models`. It reports the saved effort separately as configured but not applied. All three paths fail closed without transport fallback. Setup and status make no model call and never expose credentials or endpoint values. Direct metadata proves one local HTTP attempt and a matching provider model echo, not the gateway's internal routing, upstream physical model, or billing account.

The saved policy authorizes only the root to call these planning tools. Current MCP requests provide no caller identity, so that boundary is instruction-enforced rather than server-authenticated; no-tools execution and state authorization are mechanical. Saved state compatibility is explicit: schema 1 predates Fable and Planner, schema 2 may authorize only the historical Fable Advisor shape, and schema 3 adds Planner plus the validated non-secret route extensions. Schema and policy values must be exact integers, and unknown extensions fail closed.

Fable setup defaults to `high`. Claude Code accepts `low`, `medium`, `high`, `xhigh`, and `max`; `ultra` normalizes to `max`. Direct API retains the configured value for reporting but does not apply it. Fable is asked to put `PLAN_APPROVED` or `PLAN_REVISE` first. If runtime metadata confirms Fable but the marker is omitted, the bridge returns `PLAN_REVISE` conservatively; it never infers approval from free-form review text.

A cross-provider seat normally needs:

1. a provider already defined and authenticated in the user's Codex config;
2. a personal custom agent that pins the provider, model, and effort;
3. a new task that loads that agent;
4. v2 spawn with the matching `agent_type` and `fork_turns = "none"`.

Never create provider definitions, request keys in chat, write credentials, or imply that an OpenAI login grants access to another provider.

Codex custom providers currently use the Responses wire protocol. An Anthropic Messages endpoint is not automatically compatible. Use a supported integration that the user has configured and tested, such as an appropriate Amazon Bedrock route where available.

## Planner and Advisor permissions

A task-local Planner or Advisor is planning-only by instruction. Do not claim it is mechanically read-only unless the effective child sandbox confirms that.

A saved advisor requests `sandbox_mode = "read-only"`, but live parent permission overrides may be reapplied to children. Keep the behavioral prohibition on edits and mutation even with the requested sandbox.

The Claude Fable 5 bridge is mechanically narrower than a child: its tools accept only bounded plan or review inputs; its CLI path launches Claude with safe mode and no tools, while its direct Advisor path makes one tool-free Messages request. Neither exposes an edit or shell operation. It still has open-world model access, so every call must be deliberate and self-contained.

Planner or Advisor failure is never approval. Configured seats are required for a non-trivial Executor plan unless the user explicitly marks one best-effort for the current task. Transport failure, redirect, truncation, malformed output, missing context, stale plan versions, wrong routes, missing model metadata, or an invalid model set stops Executor work by default.

Every Advisor call is fresh and stateless. The root carries the canonical current plan, numbered version, and compact cumulative findings ledger. `PLAN_REVISE` returns to the same Planner route; `PLAN_APPROVED` stops the loop. The root allows at most five Advisor reviews. Review five without approval halts with the current plan, ledger, and unresolved findings instead of silently executing.

## Goals and task lifetime

This skill does not create, start, pause, clear, or alter a Goal. If the user already runs a Goal, the routing policy works inside the same Codex delegation flow.

Even when the write API requests user-config reload, this transient installer cannot retroactively rewrite the developer policy already compiled into another task. Start a new task after setup, update, disable, or custom-agent changes.

A personal policy can be overridden by a trusted project's `.codex/config.toml` or a managed layer. Run status from the target workspace. “Policy installed” describes the user layer; “effective in this workspace” additionally confirms that no higher-precedence layer replaces the managed fields there. Neither status proves that a live child route was used.

Named profile-v2 files are separate selected user layers. The default command does not start App Server with `--profile`, so its write/readback does not verify a named profile. A profile user must inspect that layer separately and ensure it does not override the managed v2 fields, or use the task-local fallback.

## Concurrency and service tier

For v2, the effective limit is `max_concurrent_threads_per_session` and includes the root. Setup preserves an existing v2 limit; when only legacy `agents.max_threads = N` exists, it migrates to `N + 1` so child capacity stays the same. The policy still does not force a worker count. Codex should parallelize only independent slices with non-overlapping write ownership.

Child service tier can inherit from the parent when supported. There is no portable “force standard tier” spawn setting that works across current catalogs. If allowance savings are the priority, do not enable Fast/priority on the root.

## Truthful route states

Use precise language:

- `native policy installed`: managed user policy exists; activation still depends on effective workspace config;
- `policy effective`: the managed fields win in the current workspace; this is still not a live spawn;
- `pinned custom agent available`: matching role loaded, not yet used;
- `route accepted`: exact controls were accepted and validated by the current tool;
- `unverified prompt preference`: no exact control available;
- `used and confirmed`: only when the client explicitly exposes effective runtime model/provider/effort metadata;
- `inherited root — requested child model was not used`;
- `unavailable`: provider/model/selector cannot run;
- `none`: advisor disabled.

Requested text, a config file, or child prose alone is not proof that a model ran.

## Usage and savings language

Keep these concepts separate:

- **Raw tokens:** every input, cached input, output, context, and tool-result token. Subagents can increase this total.
- **Codex credits:** token usage weighted by model-specific rates.
- **Included limits:** shared five-hour usage plus any applicable weekly limits; real consumption depends on model, context, reasoning, tools, caching, tier, and plan.
- **Other-provider usage:** separate billing or allowance.

The defensible “about 65%” example is:

```text
20% Sol + 80% Luna at 20% of Sol's token credit rate
= 0.20 + (0.80 × 0.20)
= 0.36, or about 64% fewer credits before orchestration overhead
```

Never promise 65% fewer raw tokens, a fixed weekly saving, a universal monetary saving, or five times more completed work.

## Primary sources

- [OpenAI: Subagents and custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI: Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [OpenAI: Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [OpenAI: Codex pricing and usage limits](https://learn.chatgpt.com/docs/pricing)
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic: Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
