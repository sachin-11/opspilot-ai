"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { describeAuthError } from "./members-admin";
import { useOps } from "./providers";

export function AuditViewer() {
  const {baseUrl, workspace} = useOps();
  const [action, setAction] = useState("");
  const [result, setResult] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["audit-events", workspace?.id, action, result],
    enabled: Boolean(workspace),
    initialPageParam: "" as string,
    queryFn: ({pageParam}) => {
      const params = new URLSearchParams({limit: "50"});
      if (action.trim()) params.set("action", action.trim());
      if (result) params.set("result", result);
      if (pageParam) params.set("cursor", pageParam);
      return api.auditEvents(baseUrl, workspace!.id, params);
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const events = query.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Security</p><h2>Audit log</h2><p className="muted">Append-only and integrity-verified. Secrets and tokens are never recorded.</p></div></header>
    <div className="filters"><label>Action<input value={action} onChange={(event) => setAction(event.target.value)} placeholder="e.g. membership.update"/></label><label>Result<select value={result} onChange={(event) => setResult(event.target.value)}><option value="">All results</option><option value="success">Success</option><option value="denied">Denied</option><option value="failed">Failed</option></select></label></div>
    {query.isLoading ? <section className="panel" role="status">Loading audit events…</section>
      : query.isError ? <section className="panel error" role="alert">{describeAuthError(query.error)}</section>
      : !events.length ? <section className="panel empty"><h3>No audit events</h3></section>
      : <section className="panel table-wrap"><table><caption className="sr-only">Audit events</caption><thead><tr><th>Time</th><th>Action</th><th>Actor</th><th>Result</th><th>Integrity</th></tr></thead><tbody>
        {events.map((event) => <tr key={event.id}><td>{formatDate(event.occurred_at)}</td><td><code>{event.action}</code>{event.reason_code && <small className="muted"> ({event.reason_code})</small>}</td><td>{event.actor_type}{event.actor_user_id ? ` ${event.actor_user_id.slice(0, 8)}` : ""}{event.actor_api_key_id ? ` ${event.actor_api_key_id.slice(0, 8)}` : ""}</td><td><span className={`status-badge ${event.result === "success" ? "repo-active" : "repo-failed"}`}>{event.result}</span></td><td>{event.integrity_valid ? "verified" : <strong className="error">MISMATCH</strong>}</td></tr>)}
      </tbody></table>{query.hasNextPage && <button className="button" disabled={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>Load more</button>}</section>}
  </div>;
}
