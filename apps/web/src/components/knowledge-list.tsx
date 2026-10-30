"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";

export function KnowledgeList() {
  const {baseUrl, workspace} = useOps();
  const [type, setType] = useState("");
  const query = new URLSearchParams();
  if (type) query.set("document_type", type);
  const documents = useQuery({queryKey: ["knowledge", workspace?.id, type], enabled: Boolean(workspace), queryFn: () => api.knowledgeDocuments(baseUrl, workspace!.id, query)});
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Operational memory</p><h2>Knowledge base</h2><p className="muted">Private, versioned runbooks and operational documents.</p></div><div className="control-group"><Link className="button" href="/knowledge/search">Search playground</Link><Link className="button primary" href="/knowledge/new">Add document</Link></div></header>
    <div className="filters"><label>Document type<select value={type} onChange={(event) => setType(event.target.value)}><option value="">All types</option><option value="runbook">Runbook</option><option value="postmortem">Postmortem</option><option value="architecture">Architecture</option><option value="troubleshooting">Troubleshooting</option><option value="policy">Policy</option><option value="other">Other</option></select></label></div>
    {documents.isLoading ? <section className="panel" role="status">Loading knowledge documents…</section> : documents.isError ? <section className="panel error" role="alert">Knowledge documents could not be loaded.</section> : !documents.data?.length ? <section className="panel empty"><h3>No operational documents</h3><p>Add a text, Markdown, or text-based PDF runbook.</p></section> : <section className="panel table-wrap"><table><thead><tr><th>Document</th><th>Type</th><th>Service</th><th>Status</th><th>Version</th><th>Chunks</th><th>Updated</th></tr></thead><tbody>{documents.data.map((document) => <tr key={document.id}><td><Link href={`/knowledge/${document.id}`}>{document.title}</Link><small className="tag-line">{document.tags.join(" · ")}</small></td><td>{document.document_type}</td><td>{document.service_name ?? "All services"}</td><td><span className="status-badge">{document.status}</span></td><td>{document.current_version_number ?? "—"}</td><td>{document.chunk_count}</td><td>{formatDate(document.updated_at)}</td></tr>)}</tbody></table></section>}
  </div>;
}
