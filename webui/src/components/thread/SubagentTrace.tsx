import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  fetchSubagentTrace, fetchSubagentTraceDetail,
  type SubagentTraceDetail, type SubagentTraceIndex, type SubagentTraceRequest, type SubagentTraceTool,
} from "@/lib/api";

interface TraceScope {
  token: string;
  sessionKey: string;
  taskId: string;
}

type TraceEntry = SubagentTraceRequest | SubagentTraceTool;
type TraceView = "requests" | "tools";

export function SubagentTrace({ token, sessionKey, taskId, refreshRevision }: TraceScope & { refreshRevision?: number }) {
  const { t } = useTranslation();
  const [view, setView] = useState<TraceView>("tools");
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<SubagentTraceIndex | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<TraceEntry | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    const controller = new AbortController();
    fetchSubagentTrace(token, sessionKey, taskId, controller.signal).then(
      (value) => { if (!controller.signal.aborted) { setData(value); setError(false); setLoading(false); } },
      () => { if (!controller.signal.aborted) { setError(true); setLoading(false); } },
    );
    return () => controller.abort();
  }, [token, sessionKey, taskId, refresh, refreshRevision]);

  const entries: TraceEntry[] = view === "tools"
    ? (data?.tools ?? []).filter((row) => `${row.name} ${row.target} ${row.id} ${row.phase}`.toLowerCase().includes(query.trim().toLowerCase()))
    : data?.requests ?? [];
  const visible = entries.slice().reverse().slice(0, page * 40);

  return <section aria-label={t("conversations.trace.title")} className="space-y-3 px-2 pb-4 sm:px-4">
    <div className="flex flex-wrap items-center gap-1.5">
      {(["tools", "requests"] as const).map((kind) => (
        <Button key={kind} size="sm" variant={view === kind ? "secondary" : "ghost"} aria-pressed={view === kind}
          onClick={() => { setView(kind); setSelected(null); setPage(1); }}>
          {t(`conversations.trace.${kind}`)}{data ? ` (${data[kind].length})` : ""}
        </Button>
      ))}
      {refreshRevision === undefined && <Button size="sm" variant="outline" className="ms-auto" disabled={loading} onClick={() => {
        setLoading(true); setError(false); setSelected(null); setRefresh((value) => value + 1);
      }}>{t("conversations.refresh")}</Button>}
    </div>
    {loading && <p role="status" className="text-sm text-muted-foreground">{t("conversations.loading")}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{t("conversations.error")}</p>}
    {data && <>
      {view === "tools" && data.tools.length > 0 &&
        <Input value={query} placeholder={t("conversations.trace.search")} aria-label={t("conversations.trace.search")}
          onChange={(event) => { setQuery(event.target.value); setSelected(null); setPage(1); }} />}
      {view === "requests" && <>
        <p className="text-xs text-muted-foreground">{t("conversations.trace.scope")}</p>
        {data.sources.length > 0 &&
          <details className="rounded-md border px-3 py-2 text-sm">
            <summary className="cursor-pointer font-medium">{t("conversations.trace.sources")} ({data.sources.length})</summary>
            <ul className="mt-2 space-y-1 break-all font-mono text-xs">
              {data.sources.map((path) => <li key={path}>{path}</li>)}
            </ul>
          </details>}
      </>}
      {entries.length === 0 &&
        <p className="text-sm text-muted-foreground">{t(view === "requests" ? "conversations.trace.noRequests"
          : data.tools.length ? "conversations.trace.noMatches" : "conversations.trace.noTools")}</p>}
      {visible.length > 0 && <ul className="divide-y rounded-md border">
        {visible.map((entry) => {
          const expanded = selected?.index === entry.index && selected.id === entry.id;
          return <li key={entry.index}>
            <button type="button" aria-expanded={expanded} onClick={() => setSelected(expanded ? null : entry)}
              className="block w-full min-w-0 px-3 py-2 text-start text-sm hover:bg-muted focus-visible:bg-muted">
              {"model" in entry ? <>
                <span className="font-medium">#{entry.index + 1} · {entry.transport} · {entry.model}</span>
                <span className="ms-2 text-xs text-muted-foreground">{new Date(entry.created_at_ms).toLocaleString()}</span>
              </> : <>
                <span className="font-mono font-medium">{entry.name}</span>
                <span className={entry.phase === "error" ? "ms-2 text-xs text-destructive" : "ms-2 text-xs text-muted-foreground"}>
                  {entry.phase === "start" || entry.phase === "end" || entry.phase === "error"
                    ? t(`conversations.trace.phase.${entry.phase}`) : entry.phase}
                </span>
                {entry.target && <span className="block truncate font-mono text-xs text-muted-foreground" title={entry.target}>{entry.target}</span>}
              </>}
            </button>
            {expanded && <TraceDetail key={entry.id} token={token} sessionKey={sessionKey} taskId={taskId} entry={entry} />}
          </li>;
        })}
      </ul>}
      {entries.length > visible.length &&
        <Button size="sm" variant="outline" onClick={() => setPage((value) => value + 1)}>
          {t("conversations.trace.more")}
        </Button>}
    </>}
  </section>;
}

function TraceDetail({ token, sessionKey, taskId, entry }: TraceScope & { entry: TraceEntry }) {
  const { t } = useTranslation();
  const [result, setResult] = useState<SubagentTraceDetail | "error" | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    fetchSubagentTraceDetail(token, sessionKey, taskId, entry, controller.signal).then(
      (value) => { if (!controller.signal.aborted) setResult(value); },
      () => { if (!controller.signal.aborted) setResult("error"); },
    );
    return () => controller.abort();
  }, [token, sessionKey, taskId, entry, refresh]);

  let content: ReactNode;
  if (result === null) {
    content = <p role="status" className="text-sm text-muted-foreground">{t("conversations.loading")}</p>;
  } else if (result === "error") {
    content = <>
      <p role="alert" className="text-sm text-destructive">{t("conversations.error")}</p>
      <Button size="sm" variant="outline" onClick={() => {
        setResult(null); setRefresh((value) => value + 1);
      }}>{t("conversations.refresh")}</Button>
    </>;
  } else {
    content = <>
      {result.truncated && <p className="text-xs text-muted-foreground">{t("conversations.trace.truncated")}</p>}
      <pre tabIndex={0} className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-xs">{result.body}</pre>
    </>;
  }
  return <div className="space-y-2 px-3 pb-3">{content}</div>;
}
