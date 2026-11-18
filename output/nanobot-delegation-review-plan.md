# NanoBot 委派入口拆分与原生代码审查实施方案

基于 2026-11-18 的 `codex/project-worktree-retention`，HEAD `ab69af341`。目标是让主 agent 根据所需能力自动选择执行环境，并让符合条件的原生编程子任务自行完成审查、返工和复审。用户继续在原聊天里用同一个任务编号查进度、补充要求和停止任务。

## 先借鉴 Hermes 的分层

Hermes 默认的 `delegate_task` 用配置的子代理模型，未配置时继承父模型。官方 Advanced Delegation 插件可以为工具参数加 `option` 或 `profile`，让调用它的模型按任务选择配置好的路线，运行时验证选择并回报实际路线。`openai-codex` 可以是模型 provider。完整 Codex App Server 则是另一个会话执行环境，由 `/codex-runtime` 等配置选择。Hermes 的 `delegate_task` 依赖原生 agent loop，因此完整 Codex runtime 下不能使用该工具。NanoBot 借鉴的是把**选择依据写进可见的工具契约**，由调用模型选、由 gateway 校验。 [Hermes 路由插件](https://hermes-agent.nousresearch.com/docs/developer-guide/delegation-provider-plugin) · [Hermes Codex runtime](https://hermes-agent.nousresearch.com/docs/user-guide/features/codex-app-server-runtime)

Hermes Kanban 允许在同一张任务卡请求审查、退回原执行者和复审。NanoBot 可以在已有的子任务记录里表达这几个阶段。 [Hermes 审查流转](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-worker-lanes)

## 1. 拆创建入口，共用任务管理

| 模型可见入口 | 参数和能力 | 返回与后续 |
| --- | --- | --- |
| `subagent(action="create", …)` | 原生 NanoBot 执行。`task`、`label`、项目与 checkout 参数 `project/worktree/base_ref/resume_task_id/adopt_branch`，保留 `wait`、`reply_first`、`temperature`。新增 `kind=general\|code_change`，旧调用默认为 `general`。续做时复用符合原有恢复条件的 worktree，仍是一次新的原生任务。 | 保留现有同步结果与后台回执。`kind=code_change` 且使用受管 worktree 时进入原生审查。 |
| `codex_task(task, …)` | 启动 Codex 执行，带 `resume_task_id` 时续做既有 Codex 任务。接收 `task`、`label` 和适用的项目、checkout 参数。根据渠道选择等待或后台通知，保留 Codex thread 续做、运行中补充指令、问答和停止。后端专属的 Plan Mode、Goal、原生 review 待各自接入后再暴露。 | 保留当前 Codex 回执和结果结构。`wait`、`reply_first`、`temperature` 属于原生创建参数，传给新入口时由 schema 报错。 |
| `subagent(action="check\|send\|cancel", …)` | 管理两类任务。保留 `task_id/query/message/question_id` 的现有含义。按查到的任务记录选择管理路径，继续校验原会话、发送者和等待中的问题。 | 同一任务卡和引用可贯穿执行、审查与返工。原生 `send` 使用现有 inbox，Codex 后端沿用转向和等待问答的机制。 |

修改 `nanobot/agent/tools/subagent.py`，新增 `nanobot/agent/tools/codex_task.py`。共享 Codex 创建调用和回执登记，统一转给 `SubagentManager.spawn_codex`。旧的 `subagent(action="create", backend="codex", …)` 在工具边界由窄兼容分支接受并转发，旧 `backend="native"` 仍有效。新模型可见 schema 移除 `backend`，执行验证只对这两种已知旧调用使用原契约。出现创建错误时保留具体原因和原任务引用，只有用户明确指定或继续已有任务时才锁定后端。

`nanobot/agent/tools/loader.py` 的注册阶段已有 `Tool.enabled()`，可按 gateway 配置注册 `codex_task`。每轮绑定 `RequestContext` 后按持久会话、会话 ID 和 sender ID 从调用工具视图中过滤入口。采用每轮视图，同时用于工具 schema 和派发，避免修改共享的 `ToolRegistry` 或污染其 schema 缓存。执行时继续由 `spawn_codex` 校验，CLI 未安装或认证失败用现有执行路径报告。`nanobot/command/builtin.py` 的 `/codex` 改为引导同一个新入口。保留来自旧聊天历史的工具结果和任务记录。

`webui/src/lib/subagent-create-activity.ts` 目前按工具名和动作识别创建活动。补上新入口成功创建的识别，同时让失败调用继续可见。审查过程沿用任务事件和现有 `state/phase` 投影。只有需要展示新阶段时，才在现有任务卡上补简短状态文案。

## 2. 主 agent 如何选择

把下面的规则放进两个工具的描述和 `nanobot/templates/agent/tool_contract.md`。选择发生在同一次主 agent 的工具调用里。

1. 用户指定 Codex 或原生时使用指定入口。续做时先查任务，沿用其后端和 checkout。若指定后端不可用，报告具体原因。Codex 工作未确认终止时，等状态明确再决定是否另行执行，避免重复修改或提交。
2. 先判断权限、skills 和工具。依赖 NanoBot 项目工作流、登录身份、Dorado 或故障日志的任务优先原生。以仓库为中心、需要跨文件定位或反复修改和测试的工程任务，在 Codex 能访问所需仓库及工具时优先 Codex。复杂度只在满足这些条件的路线之间排序。
3. 主 agent 可以直接完成简单查询或单步操作。混合任务按实际依赖顺序交接。例如先由原生查清 Dorado 实例和失败证据，再让 Codex 修复已定位的仓库问题，并在委派文本里带上复现条件、验收条件和本轮的操作范围。

路由在现有任务记录中用 `backend`、项目、摘要和可用性结果解释。第一版保留这两个固定入口，后续有多个配置路线时再评估是否引入 Hermes 式命名 profile。

## 3. 原生 review loop

在 `SubagentManager._run_subagent()` 中，接在原生执行 `_run_admitted_subagent()` 返回之后、`record.decide()` 之前。此时任务仍持有原 worktree 和执行槽。由新增 `nanobot/agent/subagent_review.py` 处理候选改动、证据和 reviewer 判断，原生 runner 继续负责模型与工具调用。reviewer 作为同一任务内的独立模型会话执行，复用当前任务已获取的槽位，因此 `maxConcurrentSubagents=1` 时也能工作。

1. **确定范围。** 第一版覆盖 `kind=code_change` 且使用受管 worktree 的原生任务。新任务在 worktree 初始化后记录基线。对已续做的任务使用可信的既有基线和当前任务范围，基线缺失或已有无关改动导致范围不明时，记为 `inconclusive` 并保留候选结果。无代码变动时按原要求判断，有证据表明需求已经满足就报告无改动和验证依据，否则退回执行者补证据或完成缺失改动。没有受管 worktree 的原生代码任务保留原有执行能力，并在结果里说明未走自动审查。增加 `tools.subagent.nativeReview.enabled` 作为 rollout 开关，启用时自动覆盖上述范围。
2. **取证并审查。** 在执行器正常交付候选结果、工作区写入已停止后，从基线提取实际改动。候选指纹覆盖已提交、暂存、未暂存和与任务相关的非忽略新增文件。基线、文件名、内容和读取范围由 gateway 校验。reviewer 收到任务要求、受限 diff 和完成的验证证据，必要时可用工作区内有限的文件读取能力。它没有 shell、写文件、网络型工具、MCP 或再次委派能力。测试证据首先复用 `exec_session.py` 的实际进程完成状态，以只读的任务内观察记录命令、执行目录、退出状态和候选版本。工具已经启动或模型声称全绿不足以证明测试通过。验证后又改了相关代码时，标记旧证据并要求重新核对。二进制、超限 diff 或无法覆盖的内容要在 verdict 里说明。
3. **按结构化结论流转。** reviewer 返回 `pass`、`changes_requested` 或 `inconclusive`。阻塞项包含 `requirement`、`path`、可用时的 `line`、触发条件和具体补证或修改要求。以原需求为边界，不把普通风格建议变成返工。gateway 在设置 `done` 前重查当前改动指纹和待处理的用户指令。用户中途补充要求，或者改动已变，就按新输入或新改动再判断。
4. **在原任务里修复和复审。** `changes_requested` 作为内部反馈注入原子任务，复用原执行会话和 worktree。保留原始用户要求和正常工具权限，检查证据中的文本和 reviewer 意见都不能扩大授权。修复结束审最新版本。默认最多两轮自动修复，共最多三次审查，各轮使用有界 runner 预算。首次审查不可判定、两次修复耗尽或缺少环境时，用 `incomplete` 留下候选结果、具体缺口和可继续的任务编号。取消、进程清理和 gateway 重启沿用现有生命周期，续做时从现有 checkout 重新取证，避免沿用旧审查结论。

在 `nanobot/agent/subagent_status.py` 为任务增加 `kind` 和小型 `review` 记录，使用旧记录可读的默认值。记录只需 verdict、审查轮次、候选指纹、简短问题和验证引用。运行期间用 `phase=reviewing/revising`，任务 `state` 仍为 `running`。持久化按现有 `subagent_sessions.py` 和会话内容策略走，diff 与长日志留在工作区和原有工具记录中。`nanobot/templates/agent/subagent_announce.md` 负责呈现最后执行结果、审查和验证情况；完成消息只在任务真正收尾后发出。

## 4. 实施顺序和验证

| 顺序 | 交付 | 验收 |
| --- | --- | --- |
| 1 | 新增 Codex 创建工具，保留旧路由兼容，更新 `/codex` 指令和活动显示 | 新旧入口命中同一管理路径。数据库里的旧任务可查询、停止和续做。同一 gateway 内同时处理可用和不可用的会话时，工具可见性不互相污染。 |
| 2 | 更新路由说明。在项目任务调用和测试中明确 `kind` | 用四条代表性任务执行主 agent 路由演练，分别验证轻量查询、依赖 Dorado 的诊断、复杂仓库修改、已有 Codex thread 的小修复。记录工具实际选择和理由。 |
| 3 | 接原生审查和返工，使用现有任务状态与通知 | 回归案例使用真实 Git worktree。实现者第一次返回成功且现有测试通过，但漏掉原始触发条件。reviewer 提出具体问题，原任务补测并修复，复审只接受最新版本。另验取消、审查预算耗尽和过时结论三条会影响生产行为的路径。 |

现有回归基线用 `.venv/bin/python -m pytest -q tests/agent/test_codex_tasks.py tests/agent/tools/test_subagent_tools.py tests/agent/test_subagent_project.py tests/agent/test_subagent_control.py`。新增用例优先放在同一任务和 worktree 的现有覆盖附近。涉及 WebUI 活动时，从 `webui/` 运行 `bun run test src/tests/thread-messages.test.tsx`。完成后运行 `.venv/bin/python -m scripts.check_import_boundaries`，并在开发环境分别用一次真实的原生模型和 Codex 后端跑上述代表性任务。通过标准看的是同一任务的可恢复性、版本对应关系和用户是否需要额外催促，模拟 App Server 测试只能覆盖协议和生命周期。

发布时先启用新工具和旧调用兼容层，再启用原生审查。出现审查质量或延迟问题时，用 `nativeReview.enabled` 暂停自动审查，保留已有任务历史和两个创建入口。建议在首批真实代码任务记录自动退回次数、误拦截、额外耗时和用户需要补发的指令次数，用来判断何时扩大范围。
