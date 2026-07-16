# Codex Orchestration

Bring models like Claude Fable 5 into Codex, give them roles, and make them work together.

## What does it do?

Codex Orchestration turns one Codex task into a multi-model workflow.

- Bring Fable 5 or another compatible model into Codex.
- Assign roles such as advisor, executor, researcher, writer, designer, or reviewer.
- Choose the order in which those roles work.
- Let Codex manage the handoffs and return one verified result.

The model selected for the task stays in charge. It plans, decides what feedback to use, delegates work, tests the result, and gives you the final answer.

## How it works

Here is one example:

```text
                 YOUR TASK OR GOAL
                         |
                         v
              SOL — ROOT ORCHESTRATOR
                    creates the plan
                         |
                         v
              FABLE 5 — PLAN ADVISOR
                 finds gaps and risks
                         |
                         v
              SOL — ROOT ORCHESTRATOR
             improves the plan and decides
                         |
              +----------+----------+
              |                     |
              v                     v
       LUNA EXECUTOR 1        LUNA EXECUTOR 2
          builds a part          builds a part
              |                     |
              +----------+----------+
                         |
                         v
              SOL — ROOT ORCHESTRATOR
                 tests and delivers
```

You choose the models, roles, and order. Codex follows the workflow while keeping final decisions with the root model.

## Why use it?

- **Better plans:** Fable 5 can challenge the root model before implementation begins.
- **More perspectives:** use different models for planning, research, design, writing, review, or execution.
- **Faster implementation:** independent executors can work in parallel—up to 2x faster on suitable tasks.
- **Less limit pressure:** move repeated implementation work away from the root model and potentially hit premium-model limits about 40% less often.

Results depend on the models, task, context, retries, and how much work can run in parallel. The speed and limit figures are targets, not guarantees.

## Install

```bash
codex plugin marketplace add Cjbuilds/Codex-Orchestration
codex plugin add codex-orchestration@codex-orchestration
```

Start a new Codex task after installation so the plugin loads.

Setup requires Python 3.11 or newer.

## Quick start with Fable 5

Select the model you want to lead the task, then run:

```text
/codex-orchestration setup executor: GPT-5.6 Luna Extra High, advisor: Claude Fable 5 High
```

This creates the default workflow:

```text
Selected Codex model -> Fable 5 review -> selected model decides -> Luna executes -> selected model verifies
```

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

After setup, start another new task and work normally.

## Tell Codex your workflow

Paste a workflow at the start of a task:

```text
Use this workflow:

1. The selected model is the root orchestrator and creates the plan.
2. Claude Fable 5 reviews the plan as advisor.
3. The root accepts only useful feedback and improves the plan.
4. Luna executors build independent parts in parallel.
5. The root integrates, tests, and verifies the final result.
```

You can change the sequence for any task:

```text
Researcher -> root synthesis -> designer -> writer -> reviewer -> root verification
```

## Bring another model into Codex

Ask Codex Orchestration to create a role:

```text
/codex-orchestration create these project roles:

- researcher
  model: <model-id>
  provider: <configured-provider-id>
  effort: high
  job: gather evidence and cite sources

- writer
  model: <model-id>
  effort: medium
  job: turn approved research into a clear draft

- designer
  model: <model-id>
  effort: high
  job: propose and review the user experience
```

Codex previews the role files before creating them. Project roles live in `.codex/agents/`. Personal roles live in `~/.codex/agents/` and can be used across projects.

Fable 5 is currently bundled as a plan advisor. Other models must already be available through Codex or an authenticated, compatible provider.

## Use it with Codex Goals

Set a Goal normally, then add your workflow:

```text
/goal Ship the authentication redesign with tests and migration notes.

Use my Fable advisor and Luna executor workflow until the Goal is complete.
```

Codex still owns the Goal. The plugin controls the model workflow inside it.

## Useful commands

```text
/codex-orchestration status
/codex-orchestration status --require-effective
/codex-orchestration setup executor: GPT-5.6 Luna Extra High, advisor: Claude Fable 5 High
/codex-orchestration disable
```

`disable` restores the Codex routing values that existed before setup. It does not delete user-owned custom roles.

## Important limits

- Codex remains the root orchestrator.
- Fable 5 is a root-facing plan advisor, not a second orchestrator.
- Other providers must already be configured and authenticated.
- The plugin never creates credentials or bypasses permissions and approvals.
- Codex decides when delegation or parallel work is useful.
- If you say `no subagents`, Codex must not delegate.

Technical details are in [providers and models](plugins/codex-orchestration/skills/codex-orchestration/references/providers-and-models.md).

## Update

```bash
codex plugin marketplace upgrade codex-orchestration
codex plugin add codex-orchestration@codex-orchestration
```

Start a new task after updating.

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
