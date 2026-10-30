"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, type Severity } from "@/lib/api";
import { useOps } from "./providers";

export function IncidentForm() {
  const {baseUrl, workspace} = useOps(); const router = useRouter(); const [error, setError] = useState("");
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const mutation = useMutation({mutationFn: (data: {title: string; description?: string; service_id?: string; severity: Severity}) => api.createIncident(baseUrl, workspace!.id, data), onSuccess: (incident) => router.push(`/incidents/${incident.id}`), onError: (value: Error) => setError(value.message)});
  function submit(event: FormEvent<HTMLFormElement>) { event.preventDefault(); setError(""); const data = new FormData(event.currentTarget); const title = String(data.get("title") ?? "").trim(); if (!title) {setError("Title is required."); return;} mutation.mutate({title, description: String(data.get("description") ?? "") || undefined, service_id: String(data.get("service_id") ?? "") || undefined, severity: String(data.get("severity")) as Severity}); }
  return <div className="page-stack narrow"><header className="page-header"><div><p className="eyebrow">Manual declaration</p><h2>Create incident</h2></div></header><form className="panel form" onSubmit={submit}>
    <label>Title<input name="title" maxLength={240} required /></label><label>Description<textarea name="description" rows={5} maxLength={5000} /></label>
    <label>Service<select name="service_id"><option value="">Unassigned</option>{services.data?.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.environment}</option>)}</select></label>
    <label>Severity<select name="severity" defaultValue="sev3">{["sev1","sev2","sev3","sev4"].map((x) => <option key={x}>{x}</option>)}</select></label>
    {error && <p role="alert" className="error">{error}</p>}<button className="button primary" disabled={mutation.isPending}>{mutation.isPending ? "Creating…" : "Create incident"}</button>
  </form></div>;
}
