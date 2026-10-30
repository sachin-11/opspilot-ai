"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { ApiError, api, type IncidentStatus, type Severity } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { InvestigationPanel } from "./investigation-panel";

const transitions: Record<IncidentStatus, IncidentStatus[]> = {open: ["investigating","mitigated","resolved"], investigating: ["mitigated","resolved"], mitigated: ["investigating","resolved"], resolved: ["investigating"]};

export function IncidentDetail({incidentId}: {incidentId: string}) {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient(); const [message, setMessage] = useState("");
  const key = ["incident", workspace?.id, incidentId];
  const incident = useQuery({queryKey: key, enabled: Boolean(workspace), queryFn: () => api.incident(baseUrl, workspace!.id, incidentId)});
  const timeline = useQuery({queryKey: ["timeline", workspace?.id, incidentId], enabled: Boolean(workspace), queryFn: () => api.timeline(baseUrl, workspace!.id, incidentId)});
  const refresh = async () => {await Promise.all([client.invalidateQueries({queryKey: key}), client.invalidateQueries({queryKey: ["timeline", workspace?.id, incidentId]}), client.invalidateQueries({queryKey: ["incidents", workspace?.id]})]);};
  const handleError = (error: Error) => setMessage(error instanceof ApiError && error.status === 409 ? "This incident changed elsewhere. Latest data has been reloaded." : error.message);
  const statusMutation = useMutation({mutationFn: (status: IncidentStatus) => api.status(baseUrl, workspace!.id, incidentId, status, incident.data!.version), onSuccess: refresh, onError: async (e: Error) => {handleError(e); await refresh();}});
  const severityMutation = useMutation({mutationFn: (severity: Severity) => api.severity(baseUrl, workspace!.id, incidentId, severity, incident.data!.version), onSuccess: refresh, onError: async (e: Error) => {handleError(e); await refresh();}});
  const noteMutation = useMutation({mutationFn: (summary: string) => api.note(baseUrl, workspace!.id, incidentId, summary, incident.data!.version), onSuccess: refresh, onError: async (e: Error) => {handleError(e); await refresh();}});
  if (incident.isLoading) return <p role="status">Loading incident…</p>;
  if (incident.isError || !incident.data) return <p role="alert" className="error">Incident could not be loaded.</p>;
  const item = incident.data;
  function addNote(event: FormEvent<HTMLFormElement>) {event.preventDefault(); const form = event.currentTarget; const summary = String(new FormData(form).get("summary") ?? "").trim(); if (summary) {noteMutation.mutate(summary); form.reset();}}
  return <div className="page-stack">
    <header className="detail-header"><div><p className="eyebrow">Incident · v{item.version}</p><h2>{item.title}</h2><p className="muted">{item.description || "No description provided."}</p></div><div className="badge-stack"><span className={`badge ${item.severity}`}>{item.severity.toUpperCase()}</span><span className="status-badge">{item.status}</span></div></header>
    {message && <p role="alert" className="notice">{message}</p>}
    <section className="detail-grid"><article className="panel"><h3>Response controls</h3><dl><dt>Service</dt><dd>{item.service_name ?? "Unassigned"}{item.service_environment ? ` · ${item.service_environment}` : ""}</dd><dt>Opened</dt><dd>{formatDate(item.opened_at)}</dd><dt>Acknowledged</dt><dd>{formatDate(item.acknowledged_at)}</dd><dt>Mitigated</dt><dd>{formatDate(item.mitigated_at)}</dd><dt>Resolved</dt><dd>{formatDate(item.resolved_at)}</dd></dl>
      <div className="control-group"><span>Move status</span>{transitions[item.status].map((next) => <button className="button" key={next} disabled={statusMutation.isPending} onClick={() => statusMutation.mutate(next)}>{next}</button>)}</div>
      <label>Severity<select value={item.severity} disabled={severityMutation.isPending} onChange={(e) => severityMutation.mutate(e.target.value as Severity)}>{["sev1","sev2","sev3","sev4"].map((x) => <option key={x}>{x}</option>)}</select></label>
    </article><article className="panel"><h3>Timeline</h3>{timeline.isLoading ? <p>Loading timeline…</p> : timeline.isError ? <p className="error">Timeline unavailable.</p> : <ol className="timeline">{timeline.data?.map((event) => <li key={event.id}><span className="timeline-mark"/><div><strong>{event.summary}</strong><p>{event.event_type.replaceAll("_", " ")} · {event.actor_type}</p><time>{formatDate(event.occurred_at)}</time></div></li>)}</ol>}
      <form className="note-form" onSubmit={addNote}><label>Add operational note<textarea name="summary" required maxLength={500} rows={3}/></label><button className="button primary" disabled={noteMutation.isPending}>{noteMutation.isPending ? "Adding…" : "Add note"}</button></form>
    </article></section><InvestigationPanel incidentId={incidentId} />
  </div>;
}
