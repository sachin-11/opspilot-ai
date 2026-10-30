"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, severitySchema, type SignalSummary } from "@/lib/api";
import { formatDate } from "./dashboard";
import { ObsFailure, StatusPill, shortId } from "./observability-common";
import { useOps } from "./providers";

const statuses = ["received", "published", "normalized", "processed", "duplicate", "dead_lettered", "failed"];
const ranges: [string, string, number | null][] = [["all", "All time", null], ["1h", "Last hour", 60], ["24h", "Last 24 hours", 1440], ["7d", "Last 7 days", 10080]];
type Filters = {source_id: string; service_id: string; status: string; severity: string; range: string};
const emptyFilters: Filters = {source_id: "", service_id: "", status: "", severity: "", range: "all"};

function buildQuery(filters: Filters, cursor: string | null): URLSearchParams {
  const query = new URLSearchParams({limit: "25"});
  for (const key of ["source_id", "service_id", "status", "severity"] as const) if (filters[key]) query.set(key, filters[key]);
  const minutes = ranges.find(([value]) => value === filters.range)?.[2];
  if (minutes) query.set("from", new Date(Date.now() - minutes * 60_000).toISOString());
  if (cursor) query.set("cursor", cursor);
  return query;
}

export function DuplicateBadge({signal}: {signal: SignalSummary}) {
  if (signal.status !== "duplicate" && !signal.duplicate_of) return <span className="muted">Unique</span>;
  return <span className="status-badge obs-warn" title={signal.duplicate_of ?? undefined}>duplicate{signal.duplicate_of ? ` of ${shortId(signal.duplicate_of)}` : ""}</span>;
}

function SignalDetailPanel({signalId, onClose}: {signalId: string; onClose: () => void}) {
  const {baseUrl, workspace} = useOps();
  const detail = useQuery({queryKey: ["obs-signal", workspace?.id, signalId], enabled: Boolean(workspace), queryFn: () => api.obsSignal(baseUrl, workspace!.id, signalId)});
  const data = detail.data;
  return <aside className="panel signal-detail" aria-label="Signal detail">
    <header className="section-heading"><h3>Signal {shortId(signalId)}</h3><button className="button" onClick={onClose}>Close</button></header>
    {detail.isLoading ? <p role="status">Loading signal…</p> : detail.isError ? <ObsFailure error={detail.error} what="signal detail"/> : data && <>
      <dl><dt>Title</dt><dd>{data.title ?? "—"}</dd><dt>Status</dt><dd><StatusPill status={data.status}/></dd><dt>Provider event id</dt><dd>{data.provider_event_id ?? "—"}</dd><dt>Schema</dt><dd>{data.schema_version}</dd><dt>Payload hash</dt><dd><code>{data.payload_hash}</code></dd><dt>Deduplication key</dt><dd><code>{data.deduplication_key}</code></dd><dt>Attempts</dt><dd>{data.attempt_count}</dd></dl>
      {data.failure_code && <p className="notice" role="alert"><strong>{data.failure_code}</strong>{data.failure_message ? `: ${data.failure_message}` : ""}</p>}
      <h4>Normalized payload (sanitized)</h4>
      {data.normalized ? <pre className="payload-preview"><code>{JSON.stringify(data.normalized, null, 2)}</code></pre> : <p className="empty">No normalized payload is available for this signal.</p>}
    </>}
  </aside>;
}

export function SignalExplorer() {
  const {baseUrl, workspace} = useOps();
  const [filters, setFilters] = useState<Filters>(emptyFilters); const [selected, setSelected] = useState<string | null>(null);
  const sources = useQuery({queryKey: ["obs-sources", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.obsSources(baseUrl, workspace!.id)});
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const signals = useInfiniteQuery({
    queryKey: ["obs-signals", workspace?.id, filters], enabled: Boolean(workspace), initialPageParam: null as string | null,
    queryFn: ({pageParam}) => api.obsSignals(baseUrl, workspace!.id, buildQuery(filters, pageParam)),
    getNextPageParam: (page) => page.next_cursor,
  });
  const items = signals.data?.pages.flatMap((page) => page.items) ?? [];
  const set = (key: keyof Filters) => (event: React.ChangeEvent<HTMLSelectElement>) => setFilters((current) => ({...current, [key]: event.target.value}));
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Observability</p><h2>Signals</h2><p className="muted">Every signal accepted by the pipeline, with its processing and correlation outcome.</p></div></header>
    <div className="filters" role="search" aria-label="Signal filters">
      <label>Source<select aria-label="Source" value={filters.source_id} onChange={set("source_id")}><option value="">All sources</option>{sources.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>Service<select aria-label="Service" value={filters.service_id} onChange={set("service_id")}><option value="">All services</option>{services.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <label>Status<select aria-label="Status" value={filters.status} onChange={set("status")}><option value="">All statuses</option>{statuses.map((item) => <option key={item} value={item}>{item.replaceAll("_", " ")}</option>)}</select></label>
      <label>Severity<select aria-label="Severity" value={filters.severity} onChange={set("severity")}><option value="">All severities</option>{severitySchema.options.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      <label>Time range<select aria-label="Time range" value={filters.range} onChange={set("range")}>{ranges.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    {signals.isLoading ? <p role="status">Loading signals…</p> : signals.isError && items.length === 0 ? <ObsFailure error={signals.error} what="signals"/> : items.length === 0 ? <section className="panel empty"><h3>No matching signals</h3><p>Adjust the filters, or send a webhook or generate demo signals from the overview.</p></section> : <>
      <section className="panel table-wrap"><table><thead><tr><th>Provider</th><th>Source</th><th>Service</th><th>Type</th><th>Severity</th><th>Status</th><th>Received</th><th>Occurred</th><th>Incident</th><th>Deduplication</th></tr></thead><tbody>
        {items.map((signal) => <tr key={signal.signal_id}><td>{signal.provider}</td><td>{signal.source_name ?? "—"}</td><td>{signal.service_name ?? "—"}</td>
          <td><button className="link-button" onClick={() => setSelected(signal.signal_id)} aria-label={`View signal ${signal.title ?? shortId(signal.signal_id)}`}>{signal.signal_type ?? signal.title ?? shortId(signal.signal_id)}</button></td>
          <td>{signal.severity ? <span className={`badge ${signal.severity}`}>{signal.severity.toUpperCase()}</span> : "—"}</td><td><StatusPill status={signal.status}/></td>
          <td>{formatDate(signal.received_at)}</td><td>{formatDate(signal.occurred_at)}</td>
          <td>{signal.incident_id ? <Link href={`/incidents/${signal.incident_id}`}>Incident {shortId(signal.incident_id)}</Link> : "—"}</td><td><DuplicateBadge signal={signal}/></td></tr>)}
      </tbody></table></section>
      {signals.hasNextPage && <button className="button" disabled={signals.isFetchingNextPage} onClick={() => void signals.fetchNextPage()}>{signals.isFetchingNextPage ? "Loading…" : "Load more"}</button>}
      {signals.isError && <p role="alert" className="error">More signals could not be loaded.</p>}
    </>}
    {selected && <SignalDetailPanel signalId={selected} onClose={() => setSelected(null)}/>}
  </div>;
}
