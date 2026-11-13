# nanobot 飞书项目子任务改动记录

这次改动让飞书里的 `tiktok_location_offline` 开发任务由 nanobot 原生子任务直接执行。原来的流程已经用子任务接单，但实际改代码和跑测试由它启动的 Codex CLI 完成。新流程保留飞书接单、任务编号和结果回报，由 gateway 准备独立 Git worktree，原生子任务直接在其中工作。

对比基线是修改前的 nanobot 源码 `9f7ae6fb7`，以及当时本机 `USER.md` 中的 Location 开发派发流程。第一版在本机只配置了 `tiktok_location_offline` 和当前已配对的飞书私聊。

## 原版与新版

| 能力 | 原版 | 新版 |
| --- | --- | --- |
| 执行方式 | 子任务按本机说明调用 `codex exec`，通过 Codex 会话 ID 和日志跟踪实际执行。 | 新任务用 `subagent(action="create", project="tiktok_location_offline", wait=false)`。原生子任务使用 nanobot 的文件和 shell 工具，状态由原生子会话保存。[创建入口](../nanobot/agent/tools/subagent.py) |
| 项目目录和权限 | 原生子任务继承父会话的工作区。原流程通过脚本创建 worktree，再用 Codex 的 `--cd` 和沙箱进入。 | gateway 在后台任务获得执行名额后调用配置的脚本，从项目已提交的 `HEAD` 创建独立 worktree 和分支。子任务从该 worktree 开始工作，文件访问模式和 shell 设置沿用主 Agent。主 Agent 允许访问工作区外的文件时，独立 worktree 只提供独立的默认目录。[worktree 准备](../nanobot/agent/subagent_project.py) · [绑定与执行](../nanobot/agent/subagent.py) |
| 项目规则 | 主 Agent 自动读取所选工作区根目录的 `AGENTS.md`。原生子任务启动时不自动注入项目根规则。 | 主 Agent 和配置了项目的子任务共用加载器。根目录非空的 `AGENTS.override.md` 优先，缺失或为空时才检查 `AGENTS.md`。每次提示词只注入一份根规则，子目录规则按任务需要读取。[规则加载器](../nanobot/agent/project_instructions.py) · [主 Agent 提示词](../nanobot/agent/context.py) |
| 进度推送 | 原生状态可以按需查询，`SubagentTaskChanged` 的实时转发接到了 WebUI。 | 飞书私聊也会收到项目任务的阶段消息，包括准备 worktree、就绪开始执行，以及首次观察到文件修改工具成功。排队或执行阶段持续约 90 秒时，复查任务仍在运行或排队后提醒一次。提醒随任务阶段切换或结束清理。[飞书进度订阅](../nanobot/channels/feishu/subagent_progress.py) |
| 查询和交付 | 已有 `check`、`send`、`cancel` 和完成回传。原流程还查 Codex 日志确认任务进展。 | 保留原生查询、补充指令、取消和完成回传。用短编号找到子任务 ID，通过原生状态查阶段和结果。完成回到原飞书私聊，汇报 worktree、分支、实际改动和测试结果。[控制入口](../nanobot/agent/tools/subagent.py) · [完成回报模板](../nanobot/templates/agent/subagent_announce.md) |
| 中断后续做 | 原流程按保存的 Codex 会话 ID 续跑。 | 对已有可用 worktree 的已结束或中断任务，传入 `resume_task_id` 可以在同一 worktree 启动新的原生执行。短编号可沿用，新执行有自己的 ID。gateway 重启后会保留历史，续做由用户发起。[续做入口](../nanobot/agent/subagent.py) · [中断状态处理](../nanobot/agent/subagent_sessions.py) |

## 本机配置和验收状态

- 本机 `~/.nanobot/config.json` 的 `tools.subagent.projects` 配置仓库、准备脚本和允许的飞书私聊。子任务的 shell 设置取自 `tools.exec`。本机 `~/.nanobot/workspace/USER.md` 让主 Agent 先判断是否派发。单次查询、简单检查和可快速验证的小改动由主 Agent 直接处理，需要多步骤、并行或独立 worktree 的任务再派给子 Agent。[配置定义](../nanobot/agent/tools/subagent_config.py) · [Gateway 接线](../nanobot/cli/gateway_runtime.py)
- 初版共通过 **259 项相关测试**。权限继承变更的回归测试覆盖主 Agent 有无工作区限制两种情况，并检查并行任务的 worktree 与续做路径。[项目任务测试](../tests/agent/test_subagent_project.py) · [规则测试](../tests/agent/test_context_builder.py) · [飞书进度测试](../nanobot/channels/feishu/tests/test_subagent_progress.py)
- 2026-11-12 19:46（北京时间），在隔离会话中启动真实项目子 Agent，复用原 worktree 并确认目录外的旧报告可读。通过子任务的 shell 工具执行只读 `bytedcli dorado instance get 1591530160 --region sg`，退出码为 0，Dorado 返回 `succeed`。
- 正在运行的飞书 gateway 需要重启后加载权限继承逻辑。
