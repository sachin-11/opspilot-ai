"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type Source, type SourceTestResult } from "@/lib/api";
import { formatDate } from "./dashboard";
import { ObsFailure, SecretReveal, SigningHint, StatusPill, errorMessage } from "./observability-common";
import { useOps } from "./providers";

const testLabels: Record<SourceTestResult["status"], string> = {
  ok: "Connection OK", throttled: "Throttled by provider", auth_failed: "Authentication failed", unavailable: "Provider unavailable", misconfigured: "Misconfigured",
};

function SourceCard({source, onSecret}: {source: Source; onSecret: (secret: string) => void}) {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient();
  const [test, setTest] = useState<SourceTestResult | null>(null); const [error, setError] = useState(""); const [rotating, setRotating] = useState(false);
  const refresh = () => client.invalidateQueries({queryKey: ["obs-sources", workspace?.id]});
  const toggle = useMutation({
    mutationFn: () => api.obsUpdateSource(baseUrl, workspace!.id, source.id, {status: source.status === "disabled" ? "active" : "disabled"}),
    onSuccess: refresh, onError: (e: Error) => setError(errorMessage(e)),
  });
  const run = useMutation({
    mutationFn: () => api.obsTestSource(baseUrl, workspace!.id, source.id),
    onMutate: () => { setError(""); setTest(null); },
    onSuccess: (data) => { setTest(data); },
    onError: (e: Error) => setError(errorMessage(e)),
  });
  async function rotate() {
    if (!window.confirm(`Rotate the webhook secret for "${source.name}"? The current secret stops working immediately and senders must be updated.`)) return;
    setError(""); setRotating(true);
    try {
      // Called directly (not via a cached mutation) so the secret never enters the query/mutation cache.
      const result = await api.obsRotateSecret(baseUrl, workspace!.id, source.id);
      onSecret(result.webhook_secret);
      await refresh();
    } catch (e) { setError(errorMessage(e)); } finally { setRotating(false); }
  }
  const webhookUrl = source.webhook_path ? `${baseUrl}${source.webhook_path}` : null;
  return <article className="panel source-card">
    <header className="section-heading"><div><h3>{source.name}</h3><p className="muted">{source.provider.replaceAll("_", " ")} · {source.service_name ?? "Unknown service"} · {source.environment}{source.region ? ` · ${source.region}` : ""}</p></div><StatusPill status={source.status}/></header>
    {source.status === "failing" && <p className="notice" role="alert">This source is failing. Last failure {formatDate(source.last_failure_at)}. Run a connectivity test for details.</p>}
    <dl><dt>Last event</dt><dd>{formatDate(source.last_event_at)}</dd><dt>Last success</dt><dd>{formatDate(source.last_success_at)}</dd><dt>Last failure</dt><dd>{formatDate(source.last_failure_at)}</dd></dl>
    {source.provider === "generic_webhook" && <SigningHint webhookUrl={webhookUrl}/>}
    <div className="review-actions">
      <button className="button" disabled={toggle.isPending || source.status === "failing"} onClick={() => toggle.mutate()}>{source.status === "disabled" ? "Enable" : "Disable"}</button>
      <button className="button" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Testing…" : "Test connection"}</button>
      {source.provider === "generic_webhook" && <button className="button danger" disabled={rotating} onClick={() => void rotate()}>{rotating ? "Rotating…" : "Rotate secret"}</button>}
    </div>
    {test && <p role="status" className={`notice test-result test-${test.status}`}><strong>{testLabels[test.status]}</strong> — {test.message} ({test.latency_ms} ms)</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </article>;
}

export function SourceList() {
  const {baseUrl, workspace} = useOps();
  const [revealed, setRevealed] = useState<string | null>(null);
  const sources = useQuery({queryKey: ["obs-sources", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.obsSources(baseUrl, workspace!.id)});
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Observability</p><h2>Sources</h2><p className="muted">Webhook endpoints, CloudWatch read roles, and demo fixtures that feed the pipeline.</p></div><Link className="button primary" href="/observability/sources/new">New source</Link></header>
    {revealed && <SecretReveal secret={revealed} heading="New webhook secret" onDismiss={() => setRevealed(null)}/>}
    {sources.isLoading ? <section className="panel" role="status">Loading sources…</section> : sources.isError ? <ObsFailure error={sources.error} what="sources"/> : sources.data && (sources.data.length ? <section className="card-list">{sources.data.map((source) => <SourceCard key={source.id} source={source} onSecret={setRevealed}/>)}</section> : <section className="panel empty"><h3>No sources yet</h3><p>Create a webhook, CloudWatch, or demo source to start ingesting signals.</p></section>)}
  </div>;
}
