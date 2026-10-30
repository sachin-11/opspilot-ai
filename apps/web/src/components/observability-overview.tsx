"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { ApiError, api } from "@/lib/api";
import { observabilityLiveKey } from "@/lib/realtime";
import { formatDate } from "./dashboard";
import { ObsFailure, StatusPill, errorMessage, formatMs } from "./observability-common";
import { useOps } from "./providers";

const windows = [15, 60, 360, 1440];

function Meter({label, value, max}: {label: string; value: number; max: number}) {
  return <div className="meter-row"><span>{label}</span><progress aria-label={label} value={value} max={max}/><strong>{value}</strong></div>;
}

function DemoGenerator() {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient();
  const sources = useQuery({queryKey: ["obs-sources", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.obsSources(baseUrl, workspace!.id)});
  const [sourceId, setSourceId] = useState("");
  const [result, setResult] = useState(""); const [error, setError] = useState("");
  const generate = useMutation({
    mutationFn: () => api.obsDemoGenerate(baseUrl, workspace!.id, {...(sourceId ? {source_id: sourceId} : {}), count: 20, duplicate_percent: 10, invalid_percent: 0}),
    onSuccess: async (data) => { setError(""); setResult(`Generated demo signals: ${data.accepted} accepted, ${data.duplicates} duplicates, ${data.rejected} rejected.`); await client.invalidateQueries({queryKey: ["obs-pipeline", workspace?.id]}); },
    onError: (e: Error) => { setResult(""); setError(e instanceof ApiError && e.status === 404 ? "Demo generation is disabled on this deployment (it is only available in development)." : errorMessage(e)); },
  });
  const demoSources = (sources.data ?? []).filter((item) => item.provider === "demo_fixture");
  return <section className="panel demo-generator"><h3>Generate demo signals</h3><p className="muted">Development only. Sends synthetic signals through the real pipeline. Disabled in production deployments.</p>
    <div className="filters"><label>Demo source<select value={sourceId} onChange={(e) => setSourceId(e.target.value)}><option value="">Automatic</option>{demoSources.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <button className="button" disabled={generate.isPending} onClick={() => generate.mutate()}>{generate.isPending ? "Generating…" : "Generate demo signals"}</button></div>
    {result && <p role="status" className="notice">{result}</p>}{error && <p role="alert" className="error">{error}</p>}
  </section>;
}

export function PipelineOverview() {
  const {baseUrl, workspace} = useOps(); const [windowMinutes, setWindowMinutes] = useState(60);
  const pipeline = useQuery({queryKey: ["obs-pipeline", workspace?.id, windowMinutes], enabled: Boolean(workspace), queryFn: () => api.obsPipeline(baseUrl, workspace!.id, windowMinutes), refetchInterval: 30_000});
  const live = useQuery({queryKey: observabilityLiveKey(workspace?.id ?? ""), enabled: false, staleTime: Infinity, queryFn: () => null as Record<string, unknown> | null});
  const data = pipeline.data; const event = live.data;
  const eventNote = event && typeof event.type === "string" ? `${event.type.replace("observability.", "").replaceAll("_", " ")}${typeof event.occurred_at === "string" ? ` at ${formatDate(event.occurred_at)}` : ""}` : null;
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Ingestion pipeline</p><h2>Observability</h2><p className="muted">Signed webhooks flow through a Kafka-compatible broker, are normalized, and are correlated to incidents.</p></div>
      <div className="control-group"><Link className="button" href="/observability/sources">Sources</Link><Link className="button" href="/observability/signals">Signals</Link><Link className="button" href="/observability/dead-letters">Dead letters</Link></div></header>
    <div className="filters"><label>Window<select value={windowMinutes} onChange={(e) => setWindowMinutes(Number(e.target.value))}>{windows.map((m) => <option key={m} value={m}>{m >= 60 ? `${m / 60} h` : `${m} min`}</option>)}</select></label>
      {eventNote && <p className="live-update" role="status">Live: {eventNote}</p>}</div>
    {pipeline.isLoading ? <section className="panel" role="status">Loading pipeline status…</section> : pipeline.isError ? <ObsFailure error={pipeline.error} what="pipeline status"/> : data && <>
      {data.backpressure.active && <section className="panel notice backpressure-warning" role="alert"><h3>Backpressure active</h3><p>The pipeline is shedding load. Webhook senders may receive 429 or 503 responses and should retry.</p><ul>{data.backpressure.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></section>}
      {!data.broker_healthy && <section className="panel notice" role="alert"><h3>Broker unhealthy</h3><p>The {data.broker_backend} broker is not reachable. New signals cannot be accepted until it recovers.</p></section>}
      <section className="metric-grid" aria-label="Pipeline metrics">
        <div className="metric"><span>Ingestion rate</span><strong>{data.events_per_second.toFixed(2)}/s</strong></div>
        <div className="metric"><span>Latency p50</span><strong>{formatMs(data.latency_p50_ms)}</strong></div>
        <div className="metric"><span>Latency p95</span><strong>{formatMs(data.latency_p95_ms)}</strong></div>
        <div className="metric"><span>Consumer lag</span><strong>{data.lag_available && data.consumer_lag !== null ? data.consumer_lag : "Lag unavailable"}</strong></div>
        <div className="metric"><span>Duplicates</span><strong>{data.duplicates}</strong></div>
        <div className="metric"><span>Failures</span><strong>{data.failures}</strong></div>
        <div className="metric"><span>Dead letters open</span><strong>{data.dead_letters_open}</strong></div>
        <div className="metric"><span>Broker</span><strong className={data.broker_healthy ? "obs-ok-text" : "error"}>{data.broker_healthy ? "Healthy" : "Unhealthy"}</strong><small className="muted">{data.broker_backend}</small></div>
      </section>
      <section className="panel"><h3>Outcomes in last {data.window_minutes} min</h3>
        <Meter label="Accepted" value={data.accepted} max={Math.max(data.accepted, 1)}/><Meter label="Processed" value={data.processed} max={Math.max(data.accepted, 1)}/><Meter label="Duplicates" value={data.duplicates} max={Math.max(data.accepted, 1)}/><Meter label="Failures" value={data.failures} max={Math.max(data.accepted, 1)}/>
        <p className="muted">{data.incidents_created} incidents created · {data.events_correlated} signals correlated to existing incidents · generated {formatDate(data.generated_at)}</p></section>
      <section className="panel table-wrap" aria-label="Source health"><table><thead><tr><th>Source</th><th>Provider</th><th>Status</th><th>Last event</th><th>Last success</th><th>Last failure</th></tr></thead><tbody>
        {data.sources.length === 0 ? <tr><td colSpan={6} className="empty">No sources configured. <Link href="/observability/sources/new">Add one</Link>.</td></tr> : data.sources.map((source) => <tr key={source.id}><td>{source.name}</td><td>{source.provider}</td><td><StatusPill status={source.status}/></td><td>{formatDate(source.last_event_at)}</td><td>{formatDate(source.last_success_at)}</td><td>{formatDate(source.last_failure_at)}</td></tr>)}
      </tbody></table></section>
    </>}
    <DemoGenerator/>
  </div>;
}
