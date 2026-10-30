"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useOps } from "./providers";
import { StatusDashboard } from "./status-dashboard";

export function Dashboard() {
  const {baseUrl, workspace, connection} = useOps();
  const query = useQuery({
    queryKey: ["incidents", workspace?.id, "dashboard"], enabled: Boolean(workspace),
    queryFn: () => api.incidents(baseUrl, workspace!.id, new URLSearchParams({limit: "100"})),
  });
  const incidents = query.data?.items ?? [];
  const open = incidents.filter((item) => item.status !== "resolved");
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Live operations</p><h2>Incident overview</h2></div><Link className="button primary" href="/incidents/new">New incident</Link></header>
    {query.isLoading ? <p role="status">Loading incident summary…</p> : query.isError ? <p role="alert" className="error">Incident summary unavailable.</p> : <>
      <section className="metric-grid" aria-label="Incident metrics">
        <article className="metric"><span>Active</span><strong>{open.length}</strong></article>
        {(["sev1", "sev2", "sev3", "sev4"] as const).map((sev) => <article className="metric" key={sev}><span>{sev.toUpperCase()}</span><strong>{open.filter((item) => item.severity === sev).length}</strong></article>)}
      </section>
      <section className="panel"><div className="section-heading"><h3>Recently updated</h3><span className={`connection ${connection}`}>Realtime {connection}</span></div>
        {incidents.length === 0 ? <p className="empty">No incidents recorded.</p> : <ul className="incident-rows">{incidents.slice(0, 5).map((incident) => <li key={incident.id}><Link href={`/incidents/${incident.id}`}><span className={`badge ${incident.severity}`}>{incident.severity.toUpperCase()}</span><span>{incident.title}</span><time>{formatDate(incident.updated_at)}</time></Link></li>)}</ul>}
      </section>
    </>}
    <StatusDashboard apiBaseUrl={baseUrl} />
  </div>;
}

export function formatDate(value: string | null) {
  return value ? new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(new Date(value)) : "—";
}
