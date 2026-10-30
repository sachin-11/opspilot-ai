"use client";

import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type DeadLetter } from "@/lib/api";
import { formatDate } from "./dashboard";
import { ObsFailure, errorMessage, shortId } from "./observability-common";
import { useOps } from "./providers";

function DeadLetterCard({item}: {item: DeadLetter}) {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient();
  const [pending, setPending] = useState(false); const [result, setResult] = useState(""); const [error, setError] = useState("");
  async function replay() {
    if (!window.confirm("Replay this dead-lettered signal? It will be re-published to the pipeline once. Replaying twice is safe: the second attempt is a no-op.")) return;
    setPending(true); setError(""); setResult("");
    try {
      const response = await api.obsReplay(baseUrl, workspace!.id, item.id);
      setResult(response.status === "replayed" ? `Replayed (replay count ${response.replay_count}).` : "Already replayed. Nothing further was published.");
      await client.invalidateQueries({queryKey: ["obs-dead-letters", workspace?.id]});
      await client.invalidateQueries({queryKey: ["obs-pipeline", workspace?.id]});
    } catch (e) { setError(errorMessage(e)); } finally { setPending(false); }
  }
  const retryable = item.failure_class === "retryable_exhausted";
  const done = Boolean(item.resolved_at);
  return <article className="panel dead-letter-card">
    <header className="section-heading"><div><h3>{item.failure_code}</h3><p className="muted">{item.source_name ?? "Unknown source"} · {item.original_topic}{item.signal_id ? ` · signal ${shortId(item.signal_id)}` : ""}</p></div>
      <span className={`status-badge ${retryable ? "obs-warn" : "obs-bad"}`}>{retryable ? "retryable exhausted" : "non retryable"}</span></header>
    <p className="failure-message">{item.failure_message}</p>
    <dl><dt>Attempts</dt><dd>{item.attempt_count}</dd><dt>First failure</dt><dd>{formatDate(item.first_failed_at)}</dd><dt>Last failure</dt><dd>{formatDate(item.last_failed_at)}</dd><dt>Replays</dt><dd>{item.replay_count}{item.replayed_at ? ` (last ${formatDate(item.replayed_at)})` : ""}</dd>{done && <><dt>Resolved</dt><dd>{formatDate(item.resolved_at)}</dd></>}</dl>
    {item.payload_preview ? <details><summary>Safe payload preview</summary><pre className="payload-preview"><code>{JSON.stringify(item.payload_preview, null, 2)}</code></pre></details> : <p className="muted">No payload preview retained.</p>}
    <div className="review-actions"><button className="button" disabled={!item.replayable || pending} onClick={() => void replay()}>{pending ? "Replaying…" : "Replay"}</button>{!item.replayable && <span className="muted">{retryable ? "Not replayable right now." : "Non-retryable failures need a fix to the source payload and cannot be replayed."}</span>}</div>
    {result && <p role="status" className="notice">{result}</p>}{error && <p role="alert" className="error">{error}</p>}
  </article>;
}

export function DeadLetterList() {
  const {baseUrl, workspace} = useOps();
  const letters = useInfiniteQuery({
    queryKey: ["obs-dead-letters", workspace?.id], enabled: Boolean(workspace), initialPageParam: null as string | null,
    queryFn: ({pageParam}) => { const query = new URLSearchParams({limit: "20"}); if (pageParam) query.set("cursor", pageParam); return api.obsDeadLetters(baseUrl, workspace!.id, query); },
    getNextPageParam: (page) => page.next_cursor,
  });
  const items = letters.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Observability</p><h2>Dead letters</h2><p className="muted">Signals that could not be processed. Retryable failures exhausted their attempts; non-retryable ones are invalid input.</p></div></header>
    {letters.isLoading ? <p role="status">Loading dead letters…</p> : letters.isError && items.length === 0 ? <ObsFailure error={letters.error} what="dead letters"/> : items.length === 0 ? <section className="panel empty"><h3>No dead letters</h3><p>Every accepted signal has been processed.</p></section> : <>
      <section className="card-list">{items.map((item) => <DeadLetterCard key={item.id} item={item}/>)}</section>
      {letters.hasNextPage && <button className="button" disabled={letters.isFetchingNextPage} onClick={() => void letters.fetchNextPage()}>{letters.isFetchingNextPage ? "Loading…" : "Load more"}</button>}
    </>}
  </div>;
}
