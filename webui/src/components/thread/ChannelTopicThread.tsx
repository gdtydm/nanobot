import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ThreadHeader } from "@/components/thread/ThreadHeader";
import { ThreadLoadingSkeleton } from "@/components/thread/ThreadLoadingSkeleton";
import { ThreadViewport } from "@/components/thread/ThreadViewport";
import { Button } from "@/components/ui/button";
import { useThreadVisibility } from "@/hooks/useThreadVisibility";
import { readConversation, type ConversationCursor, type ConversationPage } from "@/lib/api";
import type { UIMessage } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

export function ChannelTopicThread({ sessionKey, channel, updatedAt, title, theme, onToggleSidebar, onToggleTheme }: {
  sessionKey: string;
  channel: string;
  updatedAt?: string | null;
  title: string;
  theme: "light" | "dark";
  onToggleSidebar: () => void;
  onToggleTheme: () => void;
}) {
  const { t } = useTranslation();
  const { getToken } = useClient();
  const visible = useThreadVisibility();
  const [page, setPage] = useState<ConversationPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [olderFailed, setOlderFailed] = useState(false);
  const requestRef = useRef<AbortController | null>(null);

  const load = useCallback(async (cursor: ConversationCursor | null = null) => {
    if (cursor && requestRef.current) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(cursor === null);
    setLoadingOlder(cursor !== null);
    if (!cursor) setLoadFailed(false);
    setOlderFailed(false);
    try {
      const next = await readConversation(getToken(), sessionKey, cursor, controller.signal);
      if (controller.signal.aborted) return;
      setPage((current) => {
        if (!current || current.generation !== next.generation) return next;
        if (cursor) return { ...next, messages: [...next.messages, ...current.messages] };
        const first = next.messages[0]?.message_index;
        if (first === undefined || !current.messages.some((message) => message.message_index === first)) return next;
        const older = current.messages.filter((message) => message.message_index < first);
        return { ...next, messages: [...older, ...next.messages], next_before: current.next_before };
      });
    } catch {
      if (controller.signal.aborted) return;
      if (cursor) setOlderFailed(true);
      else setLoadFailed(true);
    } finally {
      if (!controller.signal.aborted) {
        requestRef.current = null;
        setLoading(false);
        setLoadingOlder(false);
      }
    }
  }, [getToken, sessionKey]);

  useEffect(() => {
    if (visible) void load();
    return () => {
      requestRef.current?.abort();
      requestRef.current = null;
    };
  }, [load, updatedAt, visible]);

  const messages = useMemo<UIMessage[]>(() => page?.messages.map((message) => ({
    id: `${page.generation}:${message.message_index}`,
    role: message.role,
    content: message.content,
    createdAt: typeof message.timestamp === "number"
      ? (message.timestamp < 1e12 ? message.timestamp * 1000 : message.timestamp)
      : Date.parse(message.timestamp ?? "") || 0,
  })) ?? [], [page]);

  const refresh = () => void load();
  const error = t("conversations.error");
  return (
    <section className="thread-workspace relative flex min-h-0 flex-1 flex-col overflow-hidden">
      <ThreadHeader
        title={title === channel ? title : `${channel} · ${title}`}
        onToggleSidebar={onToggleSidebar}
        theme={theme}
        onToggleTheme={onToggleTheme}
        hideSidebarToggleForHostChrome
        actions={<Button
          variant="ghost" size="icon" onClick={refresh} disabled={loading}
          aria-label={t("conversations.refresh")} title={t("conversations.refresh")}
        >
          <RefreshCw aria-hidden />
        </Button>}
      />
      {loadFailed && <p role="alert" className="px-4 py-2 text-center text-sm text-destructive">{error}</p>}
      <ThreadViewport
        conversationKey={sessionKey}
        conversationReady={page !== null}
        messages={messages}
        isStreaming={false}
        initialLoading={loading && !page}
        hasMoreBefore={page?.next_before != null}
        loadingOlder={loadingOlder}
        olderError={olderFailed ? error : null}
        onLoadOlder={() => {
          if (page?.next_before != null) {
            return load({ before: page.next_before, generation: page.generation });
          }
        }}
        emptyState={loading && !page ? <ThreadLoadingSkeleton /> : page && (
          <p className="text-sm text-muted-foreground">{t("conversations.noMessages")}</p>
        )}
      />
    </section>
  );
}
