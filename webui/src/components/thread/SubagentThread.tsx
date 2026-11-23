import { useState } from "react";
import { useTranslation } from "react-i18next";

import { SubagentTrace } from "@/components/thread/SubagentTrace";
import { SubagentThreadView } from "@/components/thread/SubagentThreadView";
import { useSubagentThread } from "@/components/thread/useSubagentThread";
import { Button } from "@/components/ui/button";
import { isSubagentReview } from "@/lib/subagent-tasks";
import type { SubagentReviewSnapshot, SubagentTaskSnapshot } from "@/lib/types";

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
  const canShowTrace = traceEnabled && task.backend !== "codex";
  const showTrace = canShowTrace && traceOpen;
  const { messages, loading, error, activeTurnId, retry } = useSubagentThread({
    token, sessionKey, task, historyEnabled: historyEnabled && !showTrace, refreshRevision,
  });
  return <>
    {task.backend === "codex" &&
      <div className="space-y-2 px-2 pb-3 text-sm sm:px-4">
        <p className="text-muted-foreground">
          {["Codex", task.reference, task.model, t(`thread.subagents.states.${task.state}`)].filter(Boolean).join(" · ")}
        </p>
        {task.progress && <p>{task.progress}</p>}
        {task.state === "waiting_input" && task.question && <p className="whitespace-pre-wrap">{task.question}</p>}
      </div>
    }
    {isSubagentReview(task.review) && <SubagentReviewDetails review={task.review} />}
    {canShowTrace &&
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

function SubagentReviewDetails({ review }: { review: SubagentReviewSnapshot }) {
  const { t } = useTranslation();
  const { summary, verdict, round, candidate, verification = [], rechecks = [] } = review;
  return <details className="mx-2 mb-3 rounded-md border px-3 py-2 text-sm sm:mx-4">
    <summary className="cursor-pointer font-medium">
      {t("thread.subagents.review.title")} · {t(`thread.subagents.review.verdict.${verdict}`)}
    </summary>
    <div className="mt-2 space-y-2 break-words">
      <p className="whitespace-pre-wrap">{summary}</p>
      {round > 0 && <p className="text-xs text-muted-foreground">
        {t("thread.subagents.review.round", { round })}{candidate && <> · <code title={candidate}>{candidate.slice(0, 12)}</code></>}
      </p>}
      {verification.length > 0 && <ol className="divide-y border-t">
        {verification.map((entry, index) => <li key={index} className="space-y-1 py-2">
          <p className="font-medium">{entry.requirement}</p>
          <p className="text-muted-foreground">{entry.detail}</p>
          <p className="text-xs text-muted-foreground">
            {t(`thread.subagents.review.source.${entry.source}`)}
            {entry.path && <> · <code>{entry.path}{entry.line ? `:${entry.line}` : ""}</code></>}
            {entry.command_id !== null && <> · {t("thread.subagents.review.command", { id: entry.command_id })}</>}
          </p>
        </li>)}
      </ol>}
      {rechecks.length > 0 && <section className="space-y-1 border-t pt-2">
        <h3 className="font-medium">{t("thread.subagents.review.previousFindings")}</h3>
        <ol className="divide-y">
          {rechecks.map((entry, index) => <li key={index} className="space-y-1 py-2">
            <p className="font-medium">
              {entry.finding.requirement} · {t(`thread.subagents.review.recheck.${entry.status}`)}
            </p>
            <p className="text-xs text-muted-foreground">{entry.finding.trigger}</p>
            <p className="text-muted-foreground">{entry.detail}</p>
          </li>)}
        </ol>
      </section>}
    </div>
  </details>;
}
