import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ConversationsDialog } from "@/components/ConversationsDialog";
import type { SubagentTaskSnapshot } from "@/lib/types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("opens a Feishu conversation and loads earlier saved messages", async () => {
  const base = "/api/webui/conversations";
  const path = `${base}/feishu.lark%3Achat`;
  const fetch = vi.fn((input: string) => {
    const body = input === base
      ? { sessions: [
        { key: "feishu.lark:chat", title: "Beacon task", preview: "", updated_at: null },
        { key: "websocket:topic", title: "Local topic", preview: "", updated_at: null },
      ] }
      : {
        generation: "history-version",
        next_before: input === path ? 2 : null,
        messages: input === path
          ? [{ message_index: 2, role: "assistant", timestamp: null, content: "Task received" }]
          : [{ message_index: 0, role: "user", timestamp: null, content: "Run the Beacon test" }],
      };
    return Promise.resolve(Response.json(body));
  });
  vi.stubGlobal("fetch", fetch);
  render(<ConversationsDialog getToken={() => "token"} onClose={() => {}} />);
  await screen.findByRole("button", { name: /Local topic/ });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "Feishu" } });
  expect(screen.queryByRole("button", { name: /Local topic/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Beacon task/ }));
  await screen.findByText("Task received");
  fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
  await screen.findByText("Run the Beacon test");
  expect(screen.getByText("Task received")).toBeTruthy();
  expect(fetch.mock.calls.map(([url]) => url)).toEqual([
    base, path, `${path}?before=2&generation=history-version`,
  ]);
});

it.each([true, false])("opens a Feishu subtask with execution details capability %s", async (showTaskTrace) => {
  const key = "feishu.lark:chat";
  const task: SubagentTaskSnapshot = {
    task_id: "task-1", label: "Beacon check", task_description: "Check Beacon", revision: 1,
    state: "done", phase: "done", elapsed_seconds: 2, iteration: 1,
    tool_events: [], usage: null, receipts: {}, result: "Task summary", partial: false,
    stop_reason: null, error: null, origin_turn_id: null, origin_message_id: null,
    created_at: 100, completed_at: 102,
  };
  const taskPath = "/api/sessions/feishu.lark%3Achat/subagents";
  const tracePath = `${taskPath}/task-1/trace`;
  const fetch = vi.fn((url: string) => {
    if (url === "/api/webui/conversations") {
      return Promise.resolve(Response.json({ sessions: [{ key, title: "Beacon", preview: "", updated_at: null }] }));
    }
    if (url === taskPath) return Promise.resolve(Response.json({ tasks: [task] }));
    if (url === tracePath) {
      return Promise.resolve(Response.json({
        sources: ["/project/AGENTS.override.md"],
        requests: [{ index: 0, id: "request-1", model: "test-model", transport: "Codex WebSocket", created_at_ms: 100000, truncated: false }],
        tools: [{ index: 0, id: "read-doc", name: "read_file", phase: "end", target: "README.md" }],
      }));
    }
    if (url === `${tracePath}?tool=0&id=read-doc`) {
      return Promise.resolve(Response.json({ body: "Contents of README.md", truncated: false }));
    }
    if (url === `${tracePath}?request=0`) {
      return Promise.resolve(Response.json({ body: '{"previous_response_id":"response_1"}', truncated: false }));
    }
    if (url === `${taskPath}/task-1/webui-thread`) {
      return Promise.resolve(Response.json({
        schemaVersion: 3, projection: "events", active_turn_id: null,
        events: [
          { event: "user_message", chat_id: "task-1", starts_turn: true, projection_id: "p", turn_id: "turn-1",
            text: "Execute the Codex CLI task", created_at_ms: 100000 },
          { event: "message", chat_id: "task-1", projection_id: "a", turn_id: "turn-1", text: "Codex command finished" },
        ],
      }));
    }
    return Promise.resolve(Response.json({ messages: [], next_before: null, generation: "1" }));
  });
  vi.stubGlobal("fetch", fetch);
  render(<ConversationsDialog getToken={() => "token"} onClose={() => {}} showTaskHistory showTaskTrace={showTaskTrace} />);
  fireEvent.click(await screen.findByRole("button", { name: /Beacon Feishu/ }));
  fireEvent.click(screen.getByRole("button", { name: "Subtasks" }));
  fireEvent.click(await screen.findByRole("button", { name: /Beacon check/ }));
  expect(await screen.findByText("Execute the Codex CLI task")).toBeVisible();
  expect(screen.getByText("Codex command finished")).toBeVisible();
  expect(fetch.mock.calls.map(([url]) => url)).toContain(`${taskPath}/task-1/webui-thread`);
  expect(fetch.mock.calls.map(([url]) => url)).not.toContain(tracePath);
  if (!showTaskTrace) {
    expect(screen.queryByRole("button", { name: "Execution details" })).toBeNull();
    return;
  }
  fireEvent.click(screen.getByRole("button", { name: "Execution details" }));
  fireEvent.click(await screen.findByRole("button", { name: /read_file.*README.md/ }));
  expect(await screen.findByText("Contents of README.md")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Model requests (1)" }));
  fireEvent.click(screen.getByText("Automatically loaded files (1)"));
  expect(screen.getByText("/project/AGENTS.override.md")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /Codex WebSocket.*test-model/ }));
  expect(await screen.findByText('{"previous_response_id":"response_1"}')).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  expect(screen.getByRole("button", { name: "Model requests (1)" })).toHaveAttribute("aria-pressed", "true");
});
