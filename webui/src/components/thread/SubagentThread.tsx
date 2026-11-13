import { useState } from "react";
import { useTranslation } from "react-i18next";

import { SubagentTrace } from "@/components/thread/SubagentTrace";
import { SubagentThreadView } from "@/components/thread/SubagentThreadView";
import { useSubagentThread } from "@/components/thread/useSubagentThread";
import { Button } from "@/components/ui/button";
import type { SubagentTaskSnapshot } from "@/lib/types";

interface SubagentThreadProps {
  token: string;
  sessionKey: string;
  task: SubagentTaskSnapshot;
  historyEnabled: boolean;
  traceEnabled?: boolean;
  refreshRevision?: number;
  isStreaming: boolean;
}

export function SubagentThread({
  token, sessionKey, task, historyEnabled, traceEnabled = false, refreshRevision, isStreaming,
}: SubagentThreadProps) {
  const { t } = useTranslation();
  const [traceOpen, setTraceOpen] = useState(false);
  const showTrace = traceEnabled && traceOpen;
  const { messages, loading, error, activeTurnId, retry } = useSubagentThread({
    token, sessionKey, task, historyEnabled: historyEnabled && !showTrace, refreshRevision,
  });
  return <>
    {traceEnabled &&
      <div className="flex px-2 pb-3 sm:px-4">
        <Button size="sm" variant="outline" aria-expanded={showTrace} onClick={() => setTraceOpen((open) => !open)}>
          {t("conversations.trace.title")}
        </Button>
      </div>}
    {showTrace
      ? <SubagentTrace key={`${sessionKey}:${task.task_id}:${token}`} token={token} sessionKey={sessionKey}
          taskId={task.task_id} refreshRevision={refreshRevision} />
      : <SubagentThreadView messages={messages} loading={loading} error={error} isStreaming={isStreaming}
          activeTurnId={activeTurnId} runStartedAt={task.created_at} onRetry={retry} />}
  </>;
}
