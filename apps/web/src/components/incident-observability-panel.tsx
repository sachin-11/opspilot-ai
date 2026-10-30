"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { ObsFailure, StatusPill, shortId } from "./observability-common";
import { useOps } from "./providers";

const MAX_LOG_LINES = 50;
const fmt = (value: number | null) => (value === null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(2));

export function IncidentObservabilityPanel({incidentId}: {incidentId: string}) {
  const {baseUrl, workspace} = useOps();
  const query = useQuery({queryKey: ["obs-incident", workspace?.id, incidentId], enabled: Boolean(workspace), queryFn: () => api.incidentObservability(baseUrl, workspace!.id, incidentId)});
  const data = query.data;
  const metric = data?.metric;
  const peak = metric ? Math.max(metric.baseline_average ?? 0, metric.incident_average ?? 0, 1e-9) : 1;
  const logs = data?.logs.slice(0, MAX_LOG_LINES) ?? [];
  return <section className="panel incident-observability" aria-labelledby="incident-obs-title">
    <header className="section-heading"><div><h3 id="incident-obs-title">Observability evidence</h3><p className="muted">Signals, metrics, logs, and alarms related to this incident (read-only).</p></div></header>
    {query.isLoading ? <p role="status">Loading observability evidence…</p> : query.isError ? <ObsFailure error={query.error} what="observability evidence"/> : data && (!data.has_source ? <div className="empty"><h4>No observability source</h4><p>Connect a source for this incident&apos;s service to see signals, metrics, and logs here.</p><Link className="button" href="/observability/sources">Open sources</Link></div> : <>
      <p className="muted">Source: {data.source_name ?? "unknown"}{data.provider ? ` (${data.provider.replaceAll("_", " ")})` : ""}</p>
      {data.notice && <p className="muted">{data.notice}</p>}
      {data.unavailable && <p className="notice provider-unavailable" role="alert"><strong>Provider unavailable.</strong> {data.unavailable} Metrics, logs, and alarm context are missing; stored signals below are still accurate.</p>}
      <h4>Related signals</h4>
      {data.signals.length ? <ul className="activity-list obs-signal-list">{data.signals.map((signal) => <li key={signal.signal_id}><code>{shortId(signal.signal_id)}</code><div><strong>{signal.title ?? signal.signal_type ?? "Signal"}</strong> <StatusPill status={signal.status}/>{signal.severity && <span className={`badge ${signal.severity}`}>{signal.severity.toUpperCase()}</span>}<p className="muted">{formatDate(signal.occurred_at ?? signal.received_at)}{signal.duplicate_of ? ` · duplicate of ${shortId(signal.duplicate_of)}` : ""}</p><p className="citations-line"><span className="citation-label">{`[Signal: ${signal.source_name ?? "source"} ${shortId(signal.signal_id)}]`}</span></p></div></li>)}</ul> : <p className="empty">No signals correlated to this incident.</p>}
      {metric && <><h4>Metric comparison</h4>
        <div className="metric-compare"><p><strong>{metric.metric_name}</strong> ({metric.statistic}{metric.unit ? `, ${metric.unit}` : ""})</p>
          <div className="meter-row"><span>Baseline avg</span><progress aria-label="Baseline average" value={Math.max(metric.baseline_average ?? 0, 0)} max={peak}/><strong>{fmt(metric.baseline_average)}</strong></div>
          <div className="meter-row"><span>Incident avg</span><progress aria-label="Incident average" value={Math.max(metric.incident_average ?? 0, 0)} max={peak}/><strong>{fmt(metric.incident_average)}</strong></div>
          <p>Change: <strong className={metric.change_percent !== null && metric.change_percent > 0 ? "error" : ""}>{metric.change_percent === null ? "not computable" : `${metric.change_percent > 0 ? "+" : ""}${metric.change_percent.toFixed(1)}%`}</strong> <small className="muted">({metric.baseline_points.length} baseline / {metric.incident_points.length} incident points)</small></p>
          <p className="citations-line"><span className="citation-label">{`[Metric: ${metric.metric_name} ${metric.statistic}]`}</span></p></div></>}
      {data.alarm && <><h4>Alarm context</h4>
        <p><strong>{data.alarm.alarm_name}</strong> <StatusPill status={data.alarm.state.toLowerCase()}/>{data.alarm.threshold !== null && <span className="muted"> · threshold {data.alarm.comparison ?? ""} {data.alarm.threshold}{data.alarm.period_seconds ? ` over ${data.alarm.period_seconds}s` : ""}</span>}</p>
        {data.alarm.transitions.length > 0 && <ol className="timeline">{data.alarm.transitions.map((t, index) => <li key={`${t.at}-${index}`}><span className="timeline-mark"/><div><strong>{t.from_state ? `${t.from_state} → ` : ""}{t.to_state}</strong>{t.reason && <p>{t.reason}</p>}<time>{formatDate(t.at)}</time></div></li>)}</ol>}
        <p className="citations-line"><span className="citation-label">{`[Alarm: ${data.alarm.alarm_name}]`}</span></p></>}
      {logs.length > 0 && <><h4>Log lines</h4>
        <div className="log-lines" role="group" aria-label="Log lines"><pre><code>{logs.map((line, index) => <span className="log-line" key={index}><time>{formatDate(line.timestamp)}</time> {line.severity ? `[${line.severity}] ` : ""}{line.message}{"\n"}</span>)}</code></pre></div>
        {data.logs.length > MAX_LOG_LINES && <small className="muted">Showing the first {MAX_LOG_LINES} of {data.logs.length} lines.</small>}
        <p className="citations-line"><span className="citation-label">{`[Logs: ${data.logs[0]?.log_group ?? "log group"} ${logs.length} lines]`}</span></p></>}
    </>)}
  </section>;
}
