import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { SubagentThread } from "@/components/thread/SubagentThread";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { fetchSubagentTasks, listConversations, readConversation, type ConversationCursor, type ConversationPage, type ConversationSummary } from "@/lib/api";
import { deriveTitle } from "@/lib/format";
import { isActiveSubagentTask } from "@/lib/subagent-tasks";
import type { SubagentTaskSnapshot } from "@/lib/types";

function channelName(key: string): string {
  const channel = key.split(":", 1)[0];
  if (channel === "websocket") return "WebUI";
  if (channel === "feishu" || channel.startsWith("feishu.")) return "Feishu / Lark";
  return channel;
}

function conversationTitle(session: ConversationSummary): string {
  return session.title || deriveTitle(session.preview, channelName(session.key));
}

function timestampLabel(value: string | number | null): string {
  if (value === null) return "";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

export function ConversationsDialog({ getToken, onClose, showTaskHistory = false, showTaskTrace = false }: {
  getToken: () => string;
  onClose: () => void;
  showTaskHistory?: boolean;
  showTaskTrace?: boolean;
}) {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<ConversationSummary[] | null>(null);
  const [selected, setSelected] = useState<ConversationSummary | null>(null);
  const [query, setQuery] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (selected) return;
    const controller = new AbortController();
    listConversations(getToken(), controller.signal).then(
      (rows) => { if (!controller.signal.aborted) setSessions(rows); },
      () => { if (!controller.signal.aborted) setError(true); },
    );
    return () => controller.abort();
  }, [getToken, refresh, selected]);

  const filtered = sessions?.filter((session) => (
    [conversationTitle(session), channelName(session.key), session.preview]
      .join(" ").toLowerCase().includes(query.trim().toLowerCase())
  ));

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="h-[min(42rem,85dvh)] max-w-3xl grid-rows-[auto_minmax(0,1fr)]">
        <DialogHeader>
          <DialogTitle>{t("conversations.title")}</DialogTitle>
          <DialogDescription>{t("conversations.description")}</DialogDescription>
        </DialogHeader>
        {selected ? (
          <ConversationThread key={selected.key} session={selected} getToken={getToken}
            showTaskHistory={showTaskHistory} showTaskTrace={showTaskTrace}
            onBack={() => { setSelected(null); setError(false); }} />
        ) : (
          <div className="flex min-h-0 flex-col gap-3">
            <div className="flex gap-2">
              <Input value={query} onChange={(event) => setQuery(event.target.value)}
                aria-label={t("conversations.search")} placeholder={t("conversations.search")} />
              <Button variant="outline" onClick={() => { setError(false); setRefresh((value) => value + 1); }}>
                {t("conversations.refresh")}
              </Button>
            </div>
            <div className="min-h-0 overflow-y-auto">
              {error && <p role="alert" className="text-sm text-destructive">{t("conversations.error")}</p>}
              {!sessions && !error && <p role="status" className="text-sm text-muted-foreground">{t("conversations.loading")}</p>}
              {filtered?.length === 0 && <p className="text-sm text-muted-foreground">{t("conversations.empty")}</p>}
              <ul className="divide-y divide-border">
                {filtered?.map((session) => (
                  <li key={session.key}>
                    <button type="button" onClick={() => setSelected(session)}
                      className="block w-full rounded-md px-3 py-2 text-start hover:bg-muted focus-visible:bg-muted">
                      <span className="block truncate text-sm font-medium">{conversationTitle(session)}</span>
                      <span className="flex justify-between gap-3 text-xs text-muted-foreground">
                        <span>{channelName(session.key)}</span><span>{timestampLabel(session.updated_at)}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function ConversationThread({ session, getToken, onBack, showTaskHistory, showTaskTrace }: {
  session: ConversationSummary;
  getToken: () => string;
  onBack: () => void;
  showTaskHistory: boolean;
  showTaskTrace: boolean;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState<ConversationPage | null>(null);
  const [cursor, setCursor] = useState<ConversationCursor | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [showTasks, setShowTasks] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    readConversation(getToken(), session.key, cursor, controller.signal).then(
      (next) => {
        if (controller.signal.aborted) return;
        setPage((current) => cursor ? { ...next, messages: [...next.messages, ...(current?.messages ?? [])] } : next);
        setLoading(false);
      },
      () => {
        if (controller.signal.aborted) return;
        setError(true);
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [cursor, getToken, refresh, session.key]);

  const refreshThread = () => {
    setCursor(null);
    setError(false);
    setLoading(true);
    setRefresh((value) => value + 1);
  };

  if (showTasks) {
    return <ConversationTasks sessionKey={session.key} getToken={getToken} showTaskTrace={showTaskTrace}
      onBack={() => setShowTasks(false)} />;
  }

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <Button variant="outline" onClick={onBack}>{t("conversations.back")}</Button>
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium">{conversationTitle(session)}</h3>
        {showTaskHistory && (
          <Button variant="outline" onClick={() => setShowTasks(true)}>{t("thread.subagents.workTitle")}</Button>
        )}
        <Button variant="outline" onClick={refreshThread}>{t("conversations.refresh")}</Button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-1">
        {page && page.next_before !== null && !error && (
          <Button variant="outline" disabled={loading} onClick={() => {
            if (page.next_before === null) return;
            setLoading(true);
            setCursor({ before: page.next_before, generation: page.generation });
          }}>{t("conversations.earlier")}</Button>
        )}
        {error && <p role="alert" className="text-sm text-destructive">{t("conversations.error")}</p>}
        {loading && <p role="status" className="text-sm text-muted-foreground">{t("conversations.loading")}</p>}
        {page?.messages.length === 0 && <p className="text-sm text-muted-foreground">{t("conversations.noMessages")}</p>}
        {page?.messages.map((message) => (
          <article key={message.message_index} className="rounded-md border border-border p-3">
            <header className="mb-1 flex justify-between gap-3 text-xs text-muted-foreground">
              <span>{t(`conversations.${message.role}`)}</span><span>{timestampLabel(message.timestamp)}</span>
            </header>
            <p className="whitespace-pre-wrap break-words text-sm">{message.content}</p>
          </article>
        ))}
      </div>
    </div>
  );
}

function ConversationTasks({ sessionKey, getToken, onBack, showTaskTrace }: {
  sessionKey: string;
  getToken: () => string;
  onBack: () => void;
  showTaskTrace: boolean;
}) {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState<SubagentTaskSnapshot[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    fetchSubagentTasks(getToken(), sessionKey, controller.signal).then(
      ({ tasks: rows }) => { if (!controller.signal.aborted) setTasks(rows); },
      () => { if (!controller.signal.aborted) setError(true); },
    );
    return () => controller.abort();
  }, [getToken, refresh, sessionKey]);

  const task = tasks?.find((row) => row.task_id === selectedId);
  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div className="flex min-w-0 items-center gap-2">
        <Button variant="outline" onClick={task ? () => setSelectedId(null) : onBack}>
          {t("conversations.back")}
        </Button>
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium">{task?.label ?? t("thread.subagents.workTitle")}</h3>
        <Button variant="outline" onClick={() => { setError(false); setRefresh((value) => value + 1); }}>
          {t("conversations.refresh")}
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <p role="alert" className="text-sm text-destructive">{t("conversations.error")}</p>}
        {!tasks && !error && <p role="status" className="text-sm text-muted-foreground">{t("conversations.loading")}</p>}
        {tasks?.length === 0 && <p className="text-sm text-muted-foreground">{t("conversations.noTasks")}</p>}
        {task ? (
          <SubagentThread key={task.task_id} token={getToken()} sessionKey={sessionKey} task={task}
            historyEnabled traceEnabled={showTaskTrace} refreshRevision={refresh} isStreaming={isActiveSubagentTask(task)} />
        ) : (
          <ul className="divide-y divide-border">
            {tasks?.map((row) => (
              <li key={row.task_id}>
                <button type="button" onClick={() => setSelectedId(row.task_id)}
                  className="flex w-full justify-between gap-3 rounded-md px-3 py-2 text-start hover:bg-muted focus-visible:bg-muted">
                  <span className="min-w-0 truncate text-sm font-medium">{row.label}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">{t(`thread.subagents.states.${row.state}`)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
