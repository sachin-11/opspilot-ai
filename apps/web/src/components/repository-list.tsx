"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { QueryFailure, shortSha } from "./repository-common";

export function RepositoryList() {
  const {baseUrl, workspace} = useOps();
  const [status, setStatus] = useState(""); const [serviceId, setServiceId] = useState("");
  const query = new URLSearchParams();
  if (status) query.set("status", status);
  if (serviceId) query.set("service_id", serviceId);
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const repositories = useQuery({queryKey: ["repositories", workspace?.id, status, serviceId], enabled: Boolean(workspace), queryFn: () => api.repositories(baseUrl, workspace!.id, query)});
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Code context</p><h2>Repositories</h2><p className="muted">Read-only GitHub repositories indexed for incident evidence.</p></div><div className="control-group"><Link className="button" href="/repositories/search">Code search</Link><Link className="button" href="/integrations/github">GitHub integration</Link></div></header>
    <div className="filters"><label>Sync status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option><option value="active">Active</option><option value="syncing">Syncing</option><option value="failed">Failed</option><option value="archived">Archived</option></select></label><label>Service<select value={serviceId} onChange={(event) => setServiceId(event.target.value)}><option value="">All services</option>{services.data?.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label></div>
    {repositories.isLoading ? <section className="panel" role="status">Loading repositories…</section> : repositories.isError ? <QueryFailure error={repositories.error} what="repositories"/> : !repositories.data?.length ? <section className="panel empty"><h3>No repositories</h3><p>Sync a GitHub installation to import repositories.</p><Link className="button primary" href="/integrations/github">Open GitHub integration</Link></section> : <section className="card-list">{repositories.data.map((repository) => <article className="panel repository-card" key={repository.id}>
      <header className="section-heading"><div><h3><Link href={`/repositories/${repository.id}`}>{repository.full_name}</Link></h3><p className="muted">{repository.is_private ? "Private" : "Public"} · default branch <code>{repository.default_branch}</code></p></div><span className={`status-badge repo-${repository.status}`}>{repository.status}</span></header>
      {repository.failure_message && <p className="error">{repository.failure_code ? `${repository.failure_code}: ` : ""}{repository.failure_message}</p>}
      <dl><dt>Indexed commit</dt><dd>{repository.active_snapshot ? <code>{shortSha(repository.active_snapshot.commit_sha)}</code> : "Not indexed"}</dd><dt>Files / chunks</dt><dd>{repository.active_snapshot ? `${repository.active_snapshot.file_count} files · ${repository.active_snapshot.chunk_count} chunks` : "—"}</dd><dt>Last indexed</dt><dd>{repository.active_snapshot?.completed_at ? formatDate(repository.active_snapshot.completed_at) : "—"}</dd><dt>Services</dt><dd>{repository.connected_services.length ? repository.connected_services.map((item) => item.service_name).join(", ") : "None connected"}</dd></dl>
    </article>)}</section>}
  </div>;
}
