# Codex Orchestration 中文使用手册

本文说明插件安装完成后如何启动、如何给任务、模型之间怎样协作，以及怎样判断工作流是否真的运行。

配套排障文档见 [Mac 与 Windows 配置、故障排查和 FAQ](troubleshooting.md)。

## 一句话理解

Codex 任务启动时选择的模型始终是 root。插件不会再创建一个总调度器，而是给 root 增加两条受控路线：

- Claude Fable 5：在实现前审查计划，只向 root 提出问题和修改建议。
- GPT-5.6 Luna xhigh：在 root 已经完成拆分后，执行边界清楚的独立子任务。

推荐闭环是：

```text
用户目标
  -> Sol/Terra root 制定计划
  -> Fable 5 审查计划
  -> root 采纳合理意见并定稿
  -> Luna xhigh 执行独立切片
  -> root 整合、测试、验收并答复
```

root 始终保留目标判断、架构取舍、文件所有权分配、冲突处理、测试和最终答复。

## 一、每台机器只做一次的准备

### 1. 前置条件

- Python 3.11 或更高。
- 当前 Codex CLI 或 Desktop 能正常启动。
- 官方 Claude Code CLI 已安装。
- 使用 Fable 5 时，二选一：
  - Claude Code 第一方 Pro/Max 登录；
  - 已经工作的 Claude Code → CC Switch → OpenRouter 路线。

Mac 和 Windows 必须分别执行 setup。不要直接互相复制整份 `config.toml`，因为 Codex、Python 和插件启动器路径可能不同。

### 2. 安装当前 fork

本 fork 包含 CC Switch/OpenRouter 的 Fable 5 路线和 Windows UTF-8 修复：

```bash
codex plugin marketplace add ZiJinZiMing/Codex-Orchestration --ref codex/cc-switch-openrouter-fable
codex plugin add codex-orchestration@codex-orchestration
```

安装完成后，新建一个 Codex 任务。旧任务不会热加载新插件。

### 3. 配置 CC Switch/OpenRouter 路线

如果使用 CC Switch：

1. 在 CC Switch 中为 Claude 选择当前 OpenRouter provider。
2. 确保 Fable 请求模型 `claude-fable-5` 映射到 `anthropic/claude-fable-5`。
3. 确保本机回环代理监听 `127.0.0.1:15721`。
4. 保持 Claude Code 用户配置可正常使用这条路线。

插件不会把 OpenRouter key 复制到 Codex，也不会创建 provider。它只检查现有本机路线，并通过官方 `claude` CLI 调用 Fable。

### 4. 在新任务中执行 setup

对于当前推荐配置，在 Codex 中输入：

```text
/codex-orchestration setup executor: GPT-5.6 Luna Extra High, advisor: Claude Fable 5 Extra High. Use the existing CC Switch/OpenRouter loopback transport.
```

如果使用 Claude 第一方订阅路线，则去掉最后一句 CC Switch/OpenRouter transport 要求。

setup 会先预览，再写入用户层路由策略。它不会替换当前任务的 root，也不会写入凭据。

### 5. 检查状态

在准备工作的目标项目中运行：

```text
/codex-orchestration status --require-effective
```

健康状态应至少包括：

- policy installed and effective；
- executor 为 `gpt-5.6-luna@xhigh`；
- advisor 为 Claude Fable 5；
- Fable transport 为预期的 first-party 或 CC Switch/OpenRouter；
- tool namespace 为 `agents`；
- 没有 orphaned managed custom agents；
- 当前 Codex 客户端兼容。

status 只证明配置在当前 workspace 生效，不证明未来任务一定调用了 Fable 或 Luna。

### 6. 再新建一个任务

setup 后必须再新建任务，选择 Sol 或 Terra 作为 root：

- Sol：架构、困难调试、高风险迁移、复杂整合。
- Terra：日常工程任务、常规调试和成本更敏感的工作。

至此，一次性配置结束。日常任务不需要重复 setup。

## 二、日常怎样启动和使用

### 标准复杂任务

打开目标项目，新建任务，选择 root，然后粘贴下面的模板：

```text
使用已经配置好的 Codex Orchestration 工作流完成本任务。

目标：<要实现或解决什么>
输入：<代码、文件、日志或现状>
边界：<禁止修改、禁止外部写入、权限和范围>
完成标准：<测试、命令或人工验收条件>

工作流：
1. root 先检查现有实现并形成可验证计划。
2. 在实现前把完整、自包含的计划交给 Claude Fable 5 审查。
3. root 只采纳合理意见；如果 Fable 返回 PLAN_REVISE，先修正计划。
4. 仅把边界清楚、彼此独立的切片交给 GPT-5.6 Luna xhigh；不同写入任务不得拥有重叠文件。
5. child 不得创建下级 agent；任务包使用独立上下文，并包含输入、边界和验收标准。
6. root 整合结果，运行最终测试并给出结论。

Fable 不可用时停止 executor 阶段并报告；不要把模型自报当作路由证明。
```

任务提示中最重要的是目标、边界和完成标准。插件负责路线，不会替用户补齐模糊授权。

### 架构审查优先

适合架构、迁移、安全或性能决策：

```text
先由 root 写出方案、约束、替代方案和验证计划，再调用 Fable 5 做只读架构审查。
当前阶段只需要方案和决策，不启动 executor，不修改文件。
```

### 并行实现

仅当任务确实能拆成独立切片时使用：

```text
Fable 审查计划后，由 root 判断是否值得并行。
最多启动完成任务所需的最少数量 Luna executor，通常 1-3 个。
每个 executor 必须有互不重叠的文件所有权和独立验收命令。
root 负责最终整合和全量验证。
```

不要为了“使用插件”而强行并行。强耦合、上下文密集或极小任务应由 root 直接完成。

### 明确不使用 subagent

用户指令优先于持久策略：

```text
本任务不使用 subagent，也不调用 advisor。由当前 root 直接完成。
```

### 配合 Codex Goal

```text
/goal <完整目标和真实关闭条件>

在 Goal 内使用已配置的 Fable advisor + Luna executor 工作流。
root 持续负责范围、验证和 Goal 的最终关闭。
```

插件不会创建、暂停、恢复或清除 Goal。

## 三、插件实际怎样完成一次任务

### Fable advisor 路线

1. root 形成自包含计划包。
2. root 调用插件启用的 `review_plan` MCP 工具。
3. 插件通过官方 Claude Code CLI 调用固定的 `claude-fable-5`。
4. CC Switch 路线经 `127.0.0.1:15721` 转发到 OpenRouter。
5. 返回必须满足结构化输出，并明确给出 `PLAN_APPROVED` 或 `PLAN_REVISE`。
6. route evidence 不完整、模型不符或 transport 失败时，一律视为 advisor unavailable，而不是批准。

Fable 只审计划，不编辑文件、不调用 shell、不创建 executor。

### Luna executor 路线

1. root 通过 `agents.spawn_agent` 创建 child。
2. 直接路线指定 `model=gpt-5.6-luna`、`reasoning_effort=xhigh`、`fork_turns=none`。
3. root 给 child 一个自包含任务包，不把完整历史无边界地复制过去。
4. child 完成自己的切片和最小验证后向 root 交接。
5. root 独立检查、整合和验收。

不同模型或 effort 的 override 不能配合完整历史 fork；当前路线使用 `fork_turns=none`。

## 四、怎样确认它真的运行了

配置状态和真实运行是两件事。

### 配置检查

```text
/codex-orchestration status --require-effective
```

这可以确认策略、workspace 覆盖、客户端兼容性和 namespace，但不能确认 live route。

### 只读烟雾测试

在一个新任务中运行：

```text
执行一次只读编排烟雾测试：
1. root 为读取当前项目 README 的首个 Markdown 标题制定计划。
2. Fable 5 必须真实审查计划，否则 FAIL。
3. 仅在审查通过后创建一个 Luna xhigh child，让它独立读取标题。
4. root 再独立读取并比较。
5. 报告 Fable 决策、Luna child 结果、root 结果和 PASS/FAIL。
不创建或修改文件，不接受模型自报代替运行证据。
```

使用 CLI 做多代理烟雾测试时，不要加 `codex exec --ephemeral`。临时任务没有可供 child 关联的持久线程，可能导致 `collab spawn failed: no thread with id`。

可信证据包括：

- Fable 工具返回的结构化 decision 和 route confirmation；
- CC Switch 同一 session 下新产生的 Fable 200 请求；
- 独立 child session 的 Luna 请求；
- root 的独立验证结果。

child 文本中写着“我是 Luna”不是机械证据。

## 五、状态、修改和停用

### 查看状态

```text
/codex-orchestration status
/codex-orchestration status --require-effective
```

### 更换 executor 或 advisor

重新运行 setup。配置器会保留最初的恢复快照，不把自己写入的值误认为用户原始配置。

### 停用

```text
/codex-orchestration disable
```

disable 会恢复 setup 前的路由字段，不删除用户自己创建的角色。停用后新建任务。

### 更新当前 fork

普通更新：

```bash
codex plugin marketplace upgrade codex-orchestration
codex plugin add codex-orchestration@codex-orchestration
```

更新后新建任务并运行 status。fork 不会自动跟随作者仓库；维护者需要先把 upstream 更新合并到 fork 分支并跑测试，详见排障文档中的更新章节。

## 六、什么时候值得使用

适合：

- 顽固 Bug 和跨模块状态追踪；
- 重构、迁移和大功能；
- 安全、性能、兼容性和架构评审；
- 能拆成独立文件或模块的并行实现；
- 需要独立反证或第二模型审查的高价值任务。

不适合：

- 一行修改、简单查找、翻译或机械格式化；
- 下一步立即依赖当前结果、无法并行的任务；
- 任务边界还没有澄清；
- 为了追求形式而创建没有独立价值的 child。

插件的价值是让不同模型在合适的位置工作，而不是让每个任务都尽可能多地调用模型。
