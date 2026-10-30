"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type IncidentStatus, type Severity } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";

export function IncidentList() {
  const {baseUrl, workspace} = useOps();
  const [status, setStatus] = useState<IncidentStatus | "">("");
  const [severity, setSeverity] = useState<Severity | "">("");
  const [service, setService] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const query = useQuery({
    queryKey: ["incidents", workspace?.id, {status, severity, service, cursor}], enabled: Boolean(workspace),
    queryFn: () => { const params = new URLSearchParams({limit: "20"}); if (status) params.set("status", status); if (severity) params.set("severity", severity); if (service) params.set("service_id", service); if (cursor) params.set("cursor", cursor); return api.incidents(baseUrl, workspace!.id, params); },
  });
  const items = query.data?.items ?? [];
  const resetCursor = () => setCursor(null);
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Response queue</p><h2>Incidents</h2></div><Link className="button primary" href="/incidents/new">New incident</Link></header>
    <section className="filters" aria-label="Incident filters">
      <label>Status<select value={status} onChange={(e) => {setStatus(e.target.value as IncidentStatus | ""); resetCursor();}}><option value="">All statuses</option>{["open","investigating","mitigated","resolved"].map((x) => <option key={x}>{x}</option>)}</select></label>
      <label>Severity<select value={severity} onChange={(e) => {setSeverity(e.target.value as Severity | ""); resetCursor();}}><option value="">All severities</option>{["sev1","sev2","sev3","sev4"].map((x) => <option key={x}>{x}</option>)}</select></label>
      <label>Service<select value={service} onChange={(e) => {setService(e.target.value); resetCursor();}}><option value="">All services</option>{services.data?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
    </section>
    {query.isLoading ? <p role="status">Loading incidents…</p> : query.isError ? <p role="alert" className="error">Could not load incidents.</p> : items.length === 0 ? <section className="panel empty"><h3>No matching incidents</h3><p>Adjust filters or create an incident.</p></section> : <section className="panel table-wrap"><table><thead><tr><th>Severity</th><th>Incident</th><th>Status</th><th>Service</th><th>Opened</th><th>Updated</th></tr></thead><tbody>{items.map((incident) => <tr key={incident.id}><td><span className={`badge ${incident.severity}`}>{incident.severity.toUpperCase()}</span></td><td><Link href={`/incidents/${incident.id}`}>{incident.title}</Link></td><td><span className="status-badge">{incident.status}</span></td><td>{incident.service_name ?? "Unassigned"}</td><td>{formatDate(incident.opened_at)}</td><td>{formatDate(incident.updated_at)}</td></tr>)}</tbody></table></section>}
    {query.data?.next_cursor && <button className="button" onClick={() => setCursor(query.data!.next_cursor)}>Next page</button>}
  </div>;
}
