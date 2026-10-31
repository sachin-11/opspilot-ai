"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { NO_AUTO_MERGE, RemediationStatusBadge, shortHash, statusLabel } from "./remediation-common";

const FILTERS = ["awaiting_approval", "approved", "publishing", "pull_request_created", "policy_blocked", "verification_failed", "stale", "failed", "rejected", "cancelled"] as const;

export function RemediationList() {
  const {baseUrl, workspace} = useOps();
  const [status, setStatus] = useState("");
  const query = new URLSearchParams();
  if (status) query.set("status", status);
  const remediations = useQuery({queryKey: ["remediations", workspace?.id, status], enabled: Boolean(workspace), queryFn: () => api.remediations(baseUrl, workspace!.id, query)});
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Controlled write path</p><h2>Remediations</h2><p className="muted">Evidence-backed, policy-checked, sandbox-verified fixes awaiting explicit human approval. {NO_AUTO_MERGE}</p></div></header>
    <div className="filters"><label>Status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option>{FILTERS.map((value) => <option key={value} value={value}>{statusLabel(value)}</option>)}</select></label></div>
    {remediations.isLoading ? <section className="panel" role="status">Loading remediations…</section>
      : remediations.isError ? <section className="panel error" role="alert">Remediations could not be loaded.</section>
      : !remediations.data?.length ? <section className="panel empty"><h3>No remediations</h3><p>Open a completed, approved investigation and choose “Propose fix”.</p><Link className="button primary" href="/incidents">Open incidents</Link></section>
      : <section className="card-list">{remediations.data.map((item) => <article className="panel" key={item.id}>
        <header className="section-heading"><div><h3><Link href={`/remediations/${item.id}`}>{item.title}</Link></h3><p className="muted">base <code>{shortHash(item.base_commit_sha)}</code> · {item.files_changed} files · +{item.lines_added}/−{item.lines_deleted} · risk {item.risk_level}</p></div><RemediationStatusBadge status={item.status}/></header>
        {item.failure_message && <p className="error">{item.failure_code ? `${item.failure_code}: ` : ""}{item.failure_message}</p>}
        <p className="muted">Updated {formatDate(item.updated_at)}{item.policy_version ? ` · policy ${item.policy_version}` : ""}</p>
      </article>)}</section>}
  </div>;
}
