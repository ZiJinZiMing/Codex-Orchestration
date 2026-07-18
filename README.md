# Codex Orchestration

Bring models like Claude Fable 5 into Codex, give each model a role, and let Codex coordinate the work.

## What is it?

Codex Orchestration adds three simple roles to a Codex task:

- **Planner** creates the plan and improves it after feedback. It is optional; when omitted, your current Codex model plans.
- **Advisor** reviews the plan, finds important gaps, and approves it when it is ready. It is optional.
- **Executor** implements the approved plan. It is required for setup.

The model selected for the Codex task remains in charge. It passes work between the roles, checks every result, and gives you the final answer.

## How it works

```text
                         YOUR TASK
                             |
                             v
                  CODEX COORDINATES THE WORK
                             |
                             v
               PLANNER CREATES THE FIRST PLAN
              Fable 5, another model, or Codex
                             |
                             v
                    ADVISOR REVIEWS IT
                       finds real gaps
                             |
                   needs work? -- yes --+
                             |            |
                            no            v
                             |      PLANNER IMPROVES IT
                             |            |
                             +<-----------+
                             |
                       PLAN APPROVED
                             |
                             v
                  EXECUTORS IMPLEMENT IT
                             |
                             v
                    CODEX TESTS & DELIVERS
```

Planner and Advisor can work through several revisions. Codex stops as soon as the Advisor returns `PLAN_APPROVED`, with a safety limit of five reviews. If approval is not reached, execution stops and Codex shows you the latest plan and unresolved issues.

## Why use it?

- Bring Fable 5 or another compatible model into Codex.
- Use different models for planning, review, and implementation.
- Get a stronger plan before code changes begin.
- Run independent implementation work in parallel—up to 2x faster on suitable tasks.
- Move repeatable work away from the root model and potentially hit premium-model limits about 40% less often.

Results depend on the models, task, context, retries, and available parallel work. The speed and limit figures are targets, not guarantees.

## Install

```bash
codex plugin marketplace add Cjbuilds/Codex-Orchestration
codex plugin add codex-orchestration@codex-orchestration
```

Start a new Codex task after installation. Setup requires Python 3.11 or newer.

## Quick start

Use Fable 5 to plan, Sol to advise, and Luna to implement:

```text
/codex-orchestration setup planner: Claude Fable 5 High, advisor: GPT-5.6 Sol High, executor: GPT-5.6 Luna Extra High
```

Or let your current Codex model plan and use Fable 5 only as Advisor:

```text
/codex-orchestration setup advisor: Claude Fable 5 High, executor: GPT-5.6 Luna Extra High
```

After setup, start another new task and use Codex normally. The saved workflow applies automatically.

Claude Fable 5 has three explicit advisor paths. The selected path is saved in routing state and printed by setup and status; paths never fall back to each other.

| Advisor path | Internal route | Configuration source |
| --- | --- | --- |
| Claude Code CLI | `claude-code` | Claude Code login or explicitly selected CLI API authentication |
| CCSwitch | `direct-api` + `user-settings` | CCSwitch-managed Claude user settings and loopback proxy |
| Python API | `direct-api` + `config-file` | Plugin-owned provider configuration under `CODEX_HOME` |

The older `direct-api` + `environment` source remains a compatibility form of the Python API path. It is used only when routing state explicitly selects `environment`; it never overrides or rescues a selected config-file path.

For a standalone Python API route, create the disabled default provider file first:

```bash
python3 <skill-dir>/scripts/configure_fable_api.py --init-default
```

It creates `CODEX_HOME/.codex-orchestration-fable-api.json` without overwriting an existing file:

```json
{
  "schema": 2,
  "provider": {
    "api_url": "https://openrouter.ai/api/v1/messages",
    "api_key": "",
    "model": "anthropic/claude-fable-5",
    "auth_type": "bearer"
  }
}
```

`api_url`, `api_key`, `model`, and `auth_type` are one provider configuration. The provider model is the exact outbound API model field and may be changed to another safe provider identifier; the orchestration identity remains Claude Fable 5. An empty `api_key` keeps the Python API path disabled and no request is made. Configure a key through the initializer's hidden prompt (never on the command line) or protect the file before editing it directly. Legacy schema-1 files remain strictly supported and are never rewritten automatically.

To replace the disabled default through the hidden key prompt while keeping the documented defaults:

```text
python3 <skill-dir>/scripts/configure_fable_api.py --force --api-url https://openrouter.ai/api/v1/messages --model anthropic/claude-fable-5 --auth-type bearer
```

Then apply the route:

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --executor-model gpt-5.6-luna \
  --executor-effort xhigh \
  --advisor-fable \
  --advisor-effort high \
  --advisor-auth-mode api \
  --advisor-api-source config-file \
  --advisor-transport direct-api \
  --apply
```

The standalone file may contain a metered credential after configuration, is never copied into routing state or tool output, and must not be committed or shared. The initializer writes it atomically and requests owner-only permissions where the operating system supports them; local administrators can still read a user-owned file, and Windows does not provide Unix `0600` semantics through Python alone.

CC Switch remains an optional alternative. Configure its OpenRouter provider with Anthropic Messages format and map Fable to `anthropic/claude-fable-5`. Let it maintain `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` in Claude Code user settings; the local Base URL may be its loopback proxy such as `http://127.0.0.1:15721`. Then apply the same direct transport with `--advisor-api-source user-settings`.

```bash
python3 <skill-dir>/scripts/configure_native_routing.py \
  --codex-bin <active-codex-binary> \
  --executor-model gpt-5.6-luna \
  --executor-effort xhigh \
  --advisor-fable \
  --advisor-effort high \
  --advisor-auth-mode api \
  --advisor-api-source user-settings \
  --advisor-transport direct-api \
  --apply
```

Plugin installation itself has no secure interactive credential hook. Python API setup refuses to continue until the provider file exists and has a non-empty key. Runtime reloads and validates the selected source for every review; missing, disabled, or invalid configuration fails closed without consulting Claude settings, CCSwitch, environment variables, or another path.

Fable defaults to **High**. With the Claude Code transport you can choose **Low**, **Medium**, **High**, **XHigh**, or **Max**; **Ultra** is accepted as an alias for Max. Direct API records the configured effort for reporting but does not send or apply it.

Fable 5 can use the official Claude Code CLI with a compatible first-party login, or an explicitly selected direct API source. With the subscription path you do not need to add an Anthropic API key to Codex. Credentials remain in the selected source and are never copied into Codex routing state.

## Choose your roles

```text
/codex-orchestration setup planner: <model and effort>, advisor: <model and effort>, executor: <model and effort>
```

- Omit `planner` to use the current Codex model as Planner.
- Omit `advisor` when you do not want plan review.
- `executor` is required.
- Planner and Advisor must use different configured model routes so the review is independent.

Role labels are literal. A model after `planner:` plans; a model after `advisor:` reviews; a model after `executor:` implements. Codex must never move a model to a different role because that model was used differently in an older plugin version. If you specify Planner and Executor but omit Advisor, the workflow has no Advisor.

Examples:

```text
/codex-orchestration setup planner: Claude Fable 5 High, advisor: GPT-5.6 Sol High, executor: GPT-5.6 Luna Extra High

/codex-orchestration setup planner: GPT-5.6 Sol Extra High, advisor: Claude Fable 5 High, executor: GPT-5.6 Luna Extra High

/codex-orchestration setup executor: GPT-5.6 Luna Extra High
```

## Bring another model into Codex

Models already available through Codex on the current provider can be assigned directly. Other models must already be available through Codex via an existing authenticated, compatible provider and a Codex custom-agent role. In all cases, cross-provider routing requires an authenticated, compatible provider.

Ask the plugin to create a project or personal role:

```text
/codex-orchestration create project role:
name: researcher
model: <exact-model-id>
provider: <configured-provider-id>
effort: high
job: gather evidence and cite sources
```

For several roles at once, start with `/codex-orchestration create these project roles:` and list each bounded role specification.

Project roles live in `.codex/agents/`. Personal roles live in `~/.codex/agents/` and can be reused across projects. Codex previews role files before creating them.

Fable 5 is the bundled cross-provider exception and can be used directly as Planner or Advisor. Fable 5 is a root-facing plan advisor, not a second orchestrator, when assigned to Advisor; when assigned to Planner, it still reports its plan only to the Codex root. The plugin never creates provider accounts, credentials, or protocol compatibility.

## Use it with Codex Goals

Create a Codex Goal normally, then tell Codex to use the saved workflow until the Goal is complete. Codex still owns Goal state, permissions, integration, and verification; the plugin only guides which models perform each role.

## Useful commands

```text
/codex-orchestration status
/codex-orchestration status --require-effective
/codex-orchestration setup planner: Claude Fable 5 High, advisor: GPT-5.6 Sol High, executor: GPT-5.6 Luna Extra High
/codex-orchestration disable
```

`disable` restores the routing values that existed before setup. It does not delete user-owned custom roles.

## Important limits

- Codex remains the root orchestrator and final authority.
- Planner and Advisor report only to Codex; they do not contact each other or Executors directly.
- The workflow reserves Fable planning tools for the root Codex model by policy. Current MCP calls do not identify their caller, so this caller boundary is instruction-enforced; the bridge itself still disables tools, edits, and session persistence.
- Advisor approval is a planning gate, not a guarantee that implementation will succeed.
- Direct model routes inherit the root provider. Other providers must already be configured and authenticated.
- The plugin never creates credentials or bypasses permissions and approvals.
- Codex decides when delegation or parallel work is useful.
- If you say `no subagents`, Codex must not delegate.

Technical details are in [providers and models](plugins/codex-orchestration/skills/codex-orchestration/references/providers-and-models.md).

## Update

```bash
codex plugin marketplace upgrade codex-orchestration
codex plugin add codex-orchestration@codex-orchestration
```

Version **0.5.3 or newer** includes Planner assignment and the direct Fable API advisor paths. It has a distinct release identity so Codex replaces the affected Advisor-only `0.5.0` cache instead of reusing it. After the two update commands, confirm `codex plugin list --json` reports `0.5.3` or newer, then start a new task; a task that already loaded the old skill cannot refresh its instructions in place.

If the version stays old or `marketplaceSource.sourceType` is `local`, Codex is pointed at a local checkout rather than the GitHub marketplace. Run `/codex-orchestration disable` first if a saved policy is active, then remove the plugin and that marketplace registration, add `Cjbuilds/Codex-Orchestration` again, and reinstall. This does not delete the local source checkout.

Before downgrading to a version older than Planner support, run `/codex-orchestration disable` with the current version first.

## Uninstall

First run:

```text
/codex-orchestration disable
```

Then remove the plugin:

```bash
codex plugin remove codex-orchestration@codex-orchestration
codex plugin marketplace remove codex-orchestration
```

Review and remove any user-owned custom roles separately.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m compileall -q plugins tests scripts
python3 -m ruff check plugins tests scripts
python3 -m unittest discover -s tests -v
python3 tests/plugin_lifecycle_smoke.py
python3 scripts/release_check.py
```

See the [production-readiness audit](docs/production-readiness-audit.md), [security policy](SECURITY.md), and [release process](RELEASE.md).

## License

MIT
