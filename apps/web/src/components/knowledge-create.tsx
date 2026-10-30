"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api } from "@/lib/api";
import { useOps } from "./providers";

const maxBytes = 5_242_880;

export function KnowledgeCreate() {
  const {baseUrl, workspace} = useOps();
  const router = useRouter();
  const [mode, setMode] = useState<"file" | "text">("text");
  const [error, setError] = useState("");
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const create = useMutation({mutationFn: (form: FormData) => api.createKnowledgeDocument(baseUrl, workspace!.id, form), onSuccess: (value) => router.push(`/knowledge/${value.document_id}`), onError: (value: Error) => setError(value.message)});
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); const form = new FormData(event.currentTarget); const title = String(form.get("title") ?? "").trim(); const slug = String(form.get("slug") ?? "").trim();
    if (!title || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug)) {setError("Title and a lowercase kebab-case slug are required."); return;}
    if (mode === "file") {const file = form.get("file"); if (!(file instanceof File) || !file.size) {setError("Choose a file to upload."); return;} if (file.size > maxBytes) {setError("File must be 5 MiB or smaller."); return;} if (!(["text/plain", "text/markdown", "application/pdf"].includes(file.type) || /\.(txt|md|markdown|pdf)$/i.test(file.name))) {setError("Only text, Markdown, and PDF files are supported."); return;} form.delete("content");} else {if (String(form.get("content") ?? "").trim().length < 20) {setError("Direct content must contain meaningful text."); return;} form.delete("file"); form.set("mime_type", "text/markdown"); form.set("filename", `${slug}.md`);}
    create.mutate(form);
  }
  return <div className="page-stack narrow"><header className="page-header"><div><p className="eyebrow">Operational memory</p><h2>Add document</h2></div></header><form className="panel form" onSubmit={submit}><label>Title<input name="title" maxLength={240} required/></label><label>Slug<input name="slug" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" placeholder="checkout-database-failures" required/></label><label>Description<textarea name="description" rows={2} maxLength={2000}/></label><div className="form-grid"><label>Document type<select name="document_type" defaultValue="runbook"><option value="runbook">Runbook</option><option value="postmortem">Postmortem</option><option value="architecture">Architecture</option><option value="troubleshooting">Troubleshooting</option><option value="policy">Policy</option><option value="other">Other</option></select></label><label>Service<select name="service_id" defaultValue=""><option value="">All services</option>{services.data?.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label></div><label>Tags<input name="tags" placeholder="checkout, database, sev1"/></label><fieldset className="mode-picker"><legend>Content source</legend><label><input type="radio" checked={mode === "text"} onChange={() => setMode("text")}/> Direct Markdown</label><label><input type="radio" checked={mode === "file"} onChange={() => setMode("file")}/> File upload</label></fieldset>{mode === "text" ? <label>Markdown or plain text<textarea name="content" rows={14} placeholder="# Recovery steps"/></label> : <label>Text, Markdown, or text-based PDF<input name="file" type="file" accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"/></label>}{error && <p className="error" role="alert">{error}</p>}<button className="button primary" disabled={create.isPending}>{create.isPending ? "Uploading…" : "Upload and process"}</button><p className="muted">Uploaded originals remain private. Extracted chunks are redacted before embedding.</p></form></div>;
}
