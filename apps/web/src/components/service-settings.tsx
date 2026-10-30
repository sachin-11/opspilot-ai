"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, type Environment } from "@/lib/api";
import { useOps } from "./providers";

export function ServiceSettings() {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient(); const [message, setMessage] = useState("");
  const query = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const mutation = useMutation({mutationFn: (data: {name: string; description?: string; environment: Environment}) => api.createService(baseUrl, workspace!.id, data), onSuccess: async () => {setMessage("Service created."); await client.invalidateQueries({queryKey: ["services", workspace?.id]});}, onError: (e: Error) => setMessage(e.message)});
  const services = query.data ?? [];
  function submit(event: FormEvent<HTMLFormElement>) {event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); mutation.mutate({name: String(data.get("name")), description: String(data.get("description")) || undefined, environment: String(data.get("environment")) as Environment}); form.reset();}
  return <div className="page-stack"><header className="page-header"><div><p className="eyebrow">Workspace configuration</p><h2>Monitored services</h2></div></header><section className="detail-grid"><article className="panel"><h3>Services</h3>{query.isLoading ? <p>Loading services…</p> : query.isError ? <p className="error">Could not load services.</p> : services.length === 0 ? <p className="empty">No monitored services yet.</p> : <ul className="service-list">{services.map((service) => <li key={service.id}><div><strong>{service.name}</strong><p>{service.description || "No description"}</p></div><span className="status-badge">{service.environment}</span></li>)}</ul>}</article>
    <form className="panel form" onSubmit={submit}><h3>Create service</h3><label>Name<input name="name" required maxLength={120}/></label><label>Description<textarea name="description" maxLength={2000} rows={3}/></label><label>Environment<select name="environment" defaultValue="production"><option>development</option><option>staging</option><option>production</option></select></label>{message && <p role="status" className="notice">{message}</p>}<button className="button primary" disabled={mutation.isPending}>{mutation.isPending ? "Creating…" : "Create service"}</button></form></section></div>;
}
