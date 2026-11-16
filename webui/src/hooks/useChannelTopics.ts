import { useEffect, useState } from "react";

import { isTopicChannel } from "@/channel-plugins/topic-channel";
import { listConversations, splitKey } from "@/lib/api";
import type { ChatSummary } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";
import { usePageVisibility } from "@/hooks/usePageVisibility";

const CHANNEL_TOPICS_REFRESH_MS = 30_000;
const EMPTY_TOPICS: ChatSummary[] = [];

export function useChannelTopics(enabled: boolean, active: boolean) {
  const { getToken } = useClient();
  const pageVisible = usePageVisibility();
  const [topics, setTopics] = useState<ChatSummary[]>(EMPTY_TOPICS);

  useEffect(() => {
    if (!enabled || !active || !pageVisible) return;
    const controller = new AbortController();
    let refreshing = false;
    const refresh = async () => {
      if (refreshing) return;
      refreshing = true;
      try {
        const rows = await listConversations(getToken(), controller.signal);
        if (controller.signal.aborted) return;
        const next = rows.flatMap((row): ChatSummary[] => {
          const identity = splitKey(row.key);
          if (!identity.chatId || !isTopicChannel(identity.channel)) return [];
          return [{
            ...identity, key: row.key, title: row.title, preview: row.preview,
            createdAt: null, updatedAt: row.updated_at,
          }];
        });
        setTopics((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
      } catch {
        // Keep the last successful list and retry while the host is visible.
      } finally {
        refreshing = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), CHANNEL_TOPICS_REFRESH_MS);
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [active, enabled, getToken, pageVisible]);

  return enabled ? topics : EMPTY_TOPICS;
}
