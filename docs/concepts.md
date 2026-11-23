# Concepts

Use this page when you want to understand nanobot before changing advanced settings. It explains the moving parts without requiring you to read the source first.

If you want source-file ownership and extension points, read [`architecture.md`](./architecture.md) after this page.

## Runtime Shape

nanobot has one small core loop and several ways to enter it:

| Part | What it does |
|---|---|
| Agent | Takes an incoming message, chooses the conversation and model, uses enabled tools when needed, and prepares a reply |
| Providers | LLM backends such as OpenRouter, Anthropic, OpenAI, Bedrock, Ollama, vLLM, and other OpenAI-compatible APIs |
| Channels | User-facing transports such as CLI, WebUI/WebSocket, Telegram, Discord, Slack, Feishu, WeChat, Email, Mattermost, and others |
| Tools | Capabilities the model may call, including files, shell, web search/fetch, MCP, cron, image generation, and subagents |
| Memory | Workspace files and session history that keep useful context across turns |
| Gateway | Long-running process that connects enabled channels and serves the health endpoint |

To test nanobot once, run `nanobot agent -m "Hello!"`; it prints a reply in your terminal. To keep nanobot running, start `nanobot gateway`. It receives messages from enabled chat apps or the WebUI and sends replies back to the same conversation.

## Config vs Workspace

The default instance lives under `~/.nanobot/`:

| Path | Meaning |
|---|---|
| `~/.nanobot/config.json` | Instance configuration: providers, model defaults, channels, tools, gateway, API, and runtime options |
| `~/.nanobot/workspace/` | Agent workspace: memory, heartbeat tasks, cron jobs, skills, and generated artifacts |
| `~/.nanobot/sessions/<workspace-id>/` | Session history stored outside the agent-accessible workspace; the opaque ID follows workspace moves |

You can override both with command flags:

```bash
nanobot onboard --config ./bot-a/config.json --workspace ./bot-a/workspace
nanobot agent --config ./bot-a/config.json --workspace ./bot-a/workspace -m "Hello"
nanobot gateway --config ./bot-a/config.json --workspace ./bot-a/workspace
```

The config file controls what nanobot may use. The workspace is where nanobot keeps state for that instance.

### Agent Workspace and Project Workspace

The configured workspace is the **agent workspace**. A WebUI chat can also select a different **project workspace** for repository-specific work without moving the agent's identity or durable state.

| Resource | Owner when a project is selected |
|---|---|
| Project instructions | A nonempty root `AGENTS.override.md`, otherwise root `AGENTS.md`, from the selected project |
| Agent profile | `SOUL.md` and `USER.md` from the agent workspace; project-local files with those names are ignored |
| Memory and custom skills | `memory/` and `skills/` from the agent workspace |
| Relative file paths and shell working directory | The selected project workspace |

When no separate project is selected, one directory normally serves both roles. Selecting a project changes the working context for that chat; it does not create a second agent or relocate the configured agent workspace.

## Config Format

`config.json` accepts both camelCase and snake_case keys. The docs use camelCase because nanobot writes config back to disk with camelCase aliases, for example `apiKey`, `modelPresets`, `intervalS`, and `maxToolResultChars`.

Most examples are partial snippets. Merge them into the existing file created by `nanobot onboard`; do not replace the whole file unless you want to reset the instance.

## One Agent Turn

A normal turn follows this flow:

1. The CLI, WebUI, or a connected chat app receives your message.
2. nanobot uses the conversation's history and the files available to that chat to prepare the request.
3. Your selected AI service generates a response. If it needs an enabled tool, nanobot runs that tool and returns the result to the model.
4. nanobot saves the reply in the conversation and shows or sends it through the same app or channel.

That flow is the same whether the message starts in the CLI, WebUI, Telegram, Discord, or another channel.

## CLI, Gateway, API, and WebUI

| Entry point | Command | Use it for |
|---|---|---|
| CLI one-shot | `nanobot agent -m "..."` | First-run checks, scripts, and quick local questions |
| CLI interactive | `nanobot agent` | Terminal chat with persistent session history |
| Gateway | `nanobot gateway` | Chat apps, WebUI, heartbeat, Dream, and long-running service mode |
| WebUI | `nanobot webui` | Prepare the local WebUI, start the gateway, and open the browser workbench |

The WebUI launcher is the normal browser entry point. Underneath, the gateway keeps the WebSocket channel and other long-running services alive. The gateway health endpoint is on `gateway.port` (`18790` by default); the browser WebUI is served on `8765` by default, not by the health endpoint.

## Provider and Model Selection

Choose and configure the primary model in **Settings → Models**, then select its preset in the chat composer. The config-file example below is an alternative for deployment-managed settings.

The active model should normally come from a named `modelPresets` entry selected by `agents.defaults.modelPreset`. Direct `agents.defaults.provider` and `agents.defaults.model` still form the implicit `default` preset for older or minimal configs. The active provider is resolved in this order:

1. If the active preset provider or implicit default provider is not `"auto"`, nanobot uses that provider.
2. If provider is `"auto"`, nanobot tries to infer the provider from the model name, configured API keys, local provider base URLs, or gateway providers.
3. OAuth providers such as OpenAI Codex and GitHub Copilot require explicit login and explicit provider/model selection inside the active preset.

Pin the provider inside the preset when setting up for the first time. It is easier to debug:

<details>
<summary>Manual configuration</summary>

```json
{
  "modelPresets": {
    "primary": {
      "provider": "openrouter",
      "model": "anthropic/claude-opus-4.5"
    }
  },
  "agents": {
    "defaults": {
      "modelPreset": "primary"
    }
  }
}
```

</details>

See [`providers.md`](./providers.md) for practical examples and [`configuration.md#providers`](./configuration.md#providers) for the full provider reference.

## Channels and Sessions

By default, each channel and chat has its own session and conversation history. The WebUI also supports multiple chats and workspace-scoped metadata for project workspaces.

## Memory, Sessions, and Dream

nanobot uses two related stores:

| Store | Location | Purpose |
|---|---|---|
| Sessions | `<config-dir>/sessions/<workspace-id>/sessions.sqlite3` | Recent conversation turns replayed into context |
| Memory | `<workspace>/memory/MEMORY.md` and `<workspace>/memory/history.jsonl` | Long-term facts and consolidated history |

Dream is a periodic consolidation job. It reads accumulated history and updates workspace memory so useful context can survive beyond short session replay.

The configured workspace contains a `.nanobot/workspace-id` file. It contains only an opaque random identifier—never conversation content or credentials. Keep it with workspace backups: it lets nanobot find the same external session namespace after the workspace is renamed, moved, or restored. A live copy opened alongside the original receives a new ID so the two workspaces do not share conversations accidentally.

See [`memory.md`](./memory.md) for the detailed design.

## Apps and Agent Plugins

Agent Plugins are installable packages that help you add and manage capabilities in nanobot. A package can include one or more of these:

| Part | Role |
|---|---|
| Agent Plugin | Installable package that can bundle skills, MCP servers, or both |
| Skill | Workflow guidance loaded progressively or invoked with `$skill-name` |
| MCP server | Runtime tools exposed to the agent |
| CLI App | Locally managed executable whose adapter is packaged and activated like a plugin |
| Apps | WebUI surface for reviewing and managing these capabilities |

Native providers, channels, built-in tools, standalone workspace skills, and directly configured MCP servers keep their existing extension paths. See [`webui.md#apps`](./webui.md#apps) for the user-facing flow and [`configuration.md#agent-plugins-v1`](./configuration.md#agent-plugins-v1) for the package contract.

## Tools and Safety

nanobot can use built-in tools and tools enabled through Apps. Common tool groups include:

- file read/write/edit and patching;
- shell execution with configurable sandboxing;
- web search and web fetch with SSRF checks;
- MCP servers;
- cron reminders, local triggers, and heartbeat tasks;
- image generation;
- subagents and runtime self-inspection.

Security-sensitive controls live in [`configuration.md#security`](./configuration.md#security). For production or shared chat apps, also configure channel access controls such as `allowFrom`, pairing, or WebSocket tokens.

## Subagents

Nanobot can delegate parts of a request to subagents while remaining responsible for the final answer. In the conversation that started the work, you can ask it to check progress, send follow-up instructions, wait for results, or cancel a task. Cancelling one task leaves the others running. A delivered instruction means the subagent received it, not that it has acted on it yet.

Background tasks return their results to the conversation that started them. Single-turn CLI and SDK calls wait for results when background delivery is unavailable. Tasks that reach their iteration limit are marked incomplete; cancelled, failed, and incomplete tasks retain available partial results.

Use `/stop` to stop the current session and its subagents. Other sessions are unaffected. Some operations may take time to stop, and cancellation does not undo file changes or other actions already taken.

Subagents belong to their original conversation. They do not appear as separate topics or in general conversation searches. Deleting the conversation stops its subagents and removes their saved history; forking it does not copy them.

Saved task history and partial results remain available after a gateway restart. In-flight execution is marked interrupted. A Codex task awaiting a decision keeps its pending question and continues when the user answers.

Gateway operators can configure projects under `tools.subagent.projects`. Each entry names a local `repository` and an optional `worktreeScript`. Any persistent conversation with subagent access can select a configured project across the WebUI, TUI, CLI, and messaging channels. Set `project` and `worktree=false` to use the existing checkout, or set `worktree=true` for an isolated checkout using `worktreeScript`. Omitting `worktree` selects isolation when the project has a setup script. A new worktree starts at the repository's local `HEAD` by default. Pass `base_ref` to start at another revision available locally, such as `origin/main`. Native subagents inherit the parent workspace access mode and shell settings, and can send setup milestones and a quiet reminder.

Project subagents discover `.agents/skills/<name>/SKILL.md` in the configured repository and, when used, the task worktree. For duplicate names, the worktree takes precedence, followed by the configured repository and then nanobot's usual skill sources. The first model request includes skill names, descriptions, and paths. The subagent reads the instructions and supporting files when needed. A restricted worktree can read the configured repository's skill directory. A configured shell sandbox also receives that directory as a read-only bind for skill scripts. Shell commands default to the selected checkout.

The gateway checks retention at startup, after successful project tasks, and hourly. Across configured projects, it keeps up to 10 clean worktrees for successful tasks and expires them seven days after their latest completion. Running and interrupted tasks, worktrees with uncommitted changes, and worktrees with local outputs (including Git-ignored outputs) stay outside that limit. Older worktrees with local files and no saved setup snapshot are also kept.

Reclaiming a checkout preserves its local branch, commits, and task history. The `subagent` tool uses `resume_task_id` to reuse the task's checkout or reopen it at the tip of its saved branch and restore its setup files. If someone switched branches in that checkout, the error reports the expected branch, current branch, and commit. The agent can pass `adopt_branch` with `resume_task_id` to continue on the named branch already checked out in the same worktree. The verified branch is then used for future continuations and reclamation.

See [Delegated work in the WebUI](./webui.md#delegated-work) for progress and task details. WebUI temporary chats do not support subagents. SDK calls with `ephemeral=True` wait for delegated results without saving the temporary conversation or its subagent history.

In the WebUI, use **Settings → Models → Native subagent model** to select a saved preset for new native tasks, including their requested fixes. The gateway config key is `tools.subagent.nativeModelPreset`. **Default (main agent)** follows the originating conversation's model when the task starts.

Native tasks marked `kind=code_change` in a managed project worktree go through automatic code review before completion. The reviewer compares the original requirements with the actual changes and completed commands. It can return issues to the same task for up to two rounds of fixes, then review the updated change. A task that exhausts this budget or cannot be reviewed keeps its partial result and can be resumed. Set `tools.subagent.nativeReview.enabled` to `false` to disable automatic review.

The reviewer uses the project rules captured before implementation. A nonempty `AGENTS.override.md` takes precedence over `AGENTS.md`. Open **Review evidence** in the WebUI task details to see static inspection, successful command evidence for the reviewed candidate, and any unverified requirements. On later rounds the reviewer checks each outstanding finding against the updated candidate.

In the WebUI, open **Settings → Models → Review model** to select a saved model preset for automatic review. You can also set `tools.subagent.nativeReview.modelPreset` to the preset name in the gateway config. **Default (main agent)** uses the model selected by the originating conversation when the task started. A selection takes effect when the next review starts, and all rounds for that task use the same selection.

### Codex tasks

Enable `tools.subagent.codex.enabled` on a gateway with an installed, authenticated Codex CLI. Set `executable` if the gateway cannot find `codex` on its PATH. Optional `model` and `effort` set the default for new tasks. The runner uses a workspace-write sandbox and sends eligible approval requests to Codex auto-review. Sandboxed commands have network access by default. Set `networkAccess` to `false` to disable network access inside the sandbox. This configuration applies to Codex tasks across conversation surfaces.

In the WebUI, use **Settings → Models → Codex subagent model** to set `tools.subagent.codex.model`. The dropdown lists models from the gateway's Codex CLI and saves your selection immediately. Choose **Default (Codex CLI)** to use the CLI's default for new threads. Tasks that continue an existing Codex thread keep the thread's saved model.

When enabled, the agent can choose Codex as the backend for a task or follow-up in a persistent conversation. You can also request Codex directly with `/codex <task>`. The original chat receives one receipt with a task reference, followed by any questions and the terminal result. Ask for progress, give a follow-up, answer a question, or stop one task by naming it or its reference. In Feishu, Telegram, and Discord, quoting the receipt identifies the task and quoting a question identifies the pending question. A question releases the execution slot. The next answer starts a turn on the same Codex thread.

The agent uses `codex_task(action="create", task=...)` to start a Codex task, and the same tool's `check`, `send`, and `cancel` actions to manage it. The `subagent` tool creates and manages only native NanoBot tasks. Each tool lists and operates on its own task type within the originating conversation; use the tool that created the task for follow-ups. To continue a terminal Codex task, call `codex_task(action="create", resume_task_id=..., task=...)`. This creates a new task entry and continues the original Codex thread when one was saved. Include the remaining work and any undelivered instructions in the new task. Verify the state of previously submitted external work before repeating it. Existing task records and IDs remain usable through the corresponding tool; callers of the former `subagent(..., backend="codex")` route must use `codex_task` instead.

Standalone CLI and SDK calls without background delivery wait for the current Codex turn to finish or ask a question. In a later call using the same persistent session, use the returned task reference to answer it. Stopping a Codex task interrupts its local turn. Remote jobs it already submitted retain their own lifecycle.

## Background Jobs

When `nanobot gateway` starts, it runs workspace-scoped automations and registers system jobs:

- `dream`, when `agents.defaults.dream.enabled` is true;
- `heartbeat`, when `gateway.heartbeat.enabled` is true.

Heartbeat reads `<workspace>/HEARTBEAT.md`. If the file has tasks under `## Active Tasks`, nanobot executes them and sends only useful/actionable results to the most recently active chat target. Routine "nothing changed" results are suppressed.

User-created reminders use the same cron service but are not the same as the protected heartbeat system job. They run as scheduled turns in their origin chat/session and normally deliver the result back to that channel.

Local triggers are also session-bound, but they do not have their own schedule. Create one from the target chat with `/trigger <name>`, then call `nanobot trigger <id> "<message>"` when a local script or external service wants nanobot to respond in that session. Webhook servers, third-party auth, and event-to-message formatting stay outside nanobot. Trigger deliveries are stored in the workspace until the linked agent turn finishes successfully. If the target session is busy, the trigger waits until that session is idle instead of being injected into the active turn. The message is recorded as an automation turn in that session. Delivery is at-least-once, so external systems should tolerate repeated trigger messages; a delivery that reaches the agent but fails is marked failed rather than retried forever.

## Where to Go Next

| Need | Read |
|---|---|
| First working install | [`quick-start.md`](./quick-start.md) |
| Provider/model setup | [`providers.md`](./providers.md) |
| Chat app setup | [`chat-apps.md`](./chat-apps.md) |
| Complete config reference | [`configuration.md`](./configuration.md) |
| Runtime debugging | [`troubleshooting.md`](./troubleshooting.md) |
