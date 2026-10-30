"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { CodeResultCard, QueryFailure, shortSha } from "./repository-common";

export function CodeEvidencePanel({incidentId}: {incidentId: string}) {
  const {baseUrl, workspace} = useOps();
  const suspects = useQuery({queryKey: ["suspect-changes", workspace?.id, incidentId], enabled: Boolean(workspace), queryFn: () => api.suspectChanges(baseUrl, workspace!.id, incidentId)});
  const data = suspects.data;
  return <section className="panel code-evidence" aria-labelledby="code-evidence-title">
    <header className="section-heading"><div><h3 id="code-evidence-title">Code evidence</h3><p className="muted">Recent changes and code related to this incident (read-only GitHub data).</p></div></header>
    <p className="notice causation-warning" role="note"><strong>Correlation does not prove causation.</strong> Suspect changes are ranked by timing and signal overlap only. Verify each one before acting.</p>
    {suspects.isLoading ? <p role="status">Loading code evidence…</p> : suspects.isError ? <QueryFailure error={suspects.error} what="code evidence"/> : data && (!data.has_repository_mapping ? <div className="empty"><h4>No repository mapping</h4><p>Map this incident&apos;s service to a repository to see suspect changes and code.</p><Link className="button" href="/repositories">Open repositories</Link></div> : <>
      {data.notice && <p className="muted">{data.notice}</p>}
      <h4>Recent deployments</h4>
      {data.deployments.length ? <ul className="activity-list">{data.deployments.map((deployment) => <li key={deployment.id}><code>{shortSha(deployment.commit_sha)}</code><div><strong>{deployment.repository_full_name} · {deployment.environment} · {deployment.status}</strong><p className="muted">{deployment.deployed_at ? formatDate(deployment.deployed_at) : "Not deployed"}</p></div></li>)}</ul> : <p className="empty">No deployments near this incident.</p>}
      <h4>Suspect changes</h4>
      {data.candidates.length ? <ol className="suspect-list">{data.candidates.map((candidate, index) => <li key={`${candidate.kind}-${candidate.commit_sha}-${index}`} className="suspect-card">
        <div className="section-heading"><strong>{candidate.title}</strong><span className={`status-badge confidence-${candidate.confidence}`}>{candidate.confidence} confidence</span></div>
        <p className="muted">{candidate.kind} <code>{shortSha(candidate.commit_sha)}</code> in {candidate.repository_full_name} · score {candidate.score.toFixed(2)} · {candidate.minutes_before_incident} min before incident</p>
        <p className="citations-line"><span className="citation-label">{candidate.citation_label}</span></p>
        {candidate.matched_signals.length > 0 && <div><small className="muted">Matched signals</small><ul className="signal-list matched">{candidate.matched_signals.map((signal) => <li key={signal}>{signal}</li>)}</ul></div>}
        {candidate.contradicting_signals.length > 0 && <div><small className="muted">Contradicting signals</small><ul className="signal-list contradicting">{candidate.contradicting_signals.map((signal) => <li key={signal}>{signal}</li>)}</ul></div>}
        {candidate.changed_files.length > 0 && <details><summary>{candidate.changed_files.length} changed file{candidate.changed_files.length === 1 ? "" : "s"}</summary><ul className="file-list">{candidate.changed_files.map((file) => <li key={file}><code>{file}</code></li>)}</ul></details>}
      </li>)}</ol> : <p className="empty">No suspect changes found in the incident window.</p>}
      {data.code_excerpts.length > 0 && <><h4>Cited code</h4><div className="search-results">{data.code_excerpts.map((excerpt) => <CodeResultCard key={excerpt.chunk_id} result={excerpt} showScores={false}/>)}</div></>}
    </>)}
  </section>;
}
