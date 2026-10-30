"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, isProviderUnavailable, type CodeSearchRequest, type CodeVariant } from "@/lib/api";
import { useOps } from "./providers";
import { CodeResultCard } from "./repository-common";

export function RepositorySearchPlayground() {
  const {baseUrl, workspace} = useOps(); const [error, setError] = useState("");
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const repositories = useQuery({queryKey: ["repositories", workspace?.id, "", ""], enabled: Boolean(workspace), queryFn: () => api.repositories(baseUrl, workspace!.id)});
  const search = useMutation({mutationFn: (data: CodeSearchRequest) => api.codeSearch(baseUrl, workspace!.id, data), onError: (value: Error) => setError(isProviderUnavailable(value) ? "Provider unavailable: code search could not reach its backing provider. Try again shortly." : value.message)});
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    const form = new FormData(event.currentTarget); const text = (name: string) => String(form.get(name) ?? "").trim();
    const query = text("query"); if (!query) {setError("Enter a search query."); return;}
    const topK = Number(form.get("top_k")); const candidateCount = Math.max(20, topK);
    search.mutate({query, repository_id: text("repository_id") || undefined, service_id: text("service_id") || undefined, language: text("language") || undefined, path_prefix: text("path_prefix") || undefined, variant: text("variant") as CodeVariant, top_k: topK, candidate_count: candidateCount});
  }
  const data = search.data;
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Engineering demo</p><h2>Code search</h2><p className="muted">Search indexed repository code with lexical, vector, RRF, and reranked retrieval. Results are read-only citations.</p></div></header>
    <form className="panel form" onSubmit={submit}><label>Query<input name="query" maxLength={1000} placeholder="Where is the checkout database pool configured?"/></label>
      <div className="form-grid"><label>Repository<select name="repository_id"><option value="">All repositories</option>{repositories.data?.map((repository) => <option key={repository.id} value={repository.id}>{repository.full_name}</option>)}</select></label><label>Service<select name="service_id"><option value="">All services</option>{services.data?.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label><label>Language<input name="language" maxLength={40} placeholder="python"/></label><label>Path prefix<input name="path_prefix" maxLength={500} placeholder="services/checkout/"/></label><label>Variant<select name="variant" defaultValue="hybrid_rrf"><option value="lexical">Lexical</option><option value="vector">Vector</option><option value="hybrid_rrf">Hybrid RRF</option><option value="hybrid_rrf_rerank">Hybrid + rerank</option></select></label><label>Top K<select name="top_k" defaultValue="5"><option value="3">3</option><option value="5">5</option><option value="10">10</option><option value="20">20</option></select></label></div>
      {error && <p role="alert" className="error">{error}</p>}
      <button className="button primary" disabled={search.isPending}>{search.isPending ? "Searching…" : "Search code"}</button></form>
    {search.isPending && <p role="status" className="muted">Searching indexed code…</p>}
    {data && <><section className="retrieval-diagnostics"><span>Variant <strong>{data.diagnostics.variant}</strong></span><span>Candidates <strong>{data.diagnostics.candidate_count}</strong></span><span>Latency <strong>{data.diagnostics.latency_ms.toFixed(1)} ms</strong></span><span>Reranker <strong>{data.diagnostics.rerank_fallback_used ? "Fallback to RRF" : "Normal"}</strong></span></section>
      {data.diagnostics.low_relevance && <section className="panel notice" role="status"><h3>Low relevance</h3><p>No result scored as clearly relevant. Treat anything below as a weak match, or refine the query, repository, or path prefix.</p></section>}
      {!data.results.length ? <section className="panel empty"><h3>No code results</h3><p>Nothing matched. Check that the repository is indexed, or broaden the filters.</p></section> : <section className="search-results">{data.results.map((result) => <CodeResultCard key={result.chunk_id} result={result}/>)}</section>}</>}
  </div>;
}
