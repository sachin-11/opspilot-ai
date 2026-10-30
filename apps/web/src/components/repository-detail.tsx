"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, type Environment, type RepositoryDetail } from "@/lib/api";
import { repositoryProgressKey } from "@/lib/realtime";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { QueryFailure, shortSha } from "./repository-common";

type Progress = {type: string; stage?: string; status?: string; files_processed?: number; total_files?: number; chunks_processed?: number; failure_code?: string};
const liveTypes = new Set(["repository.indexing_started", "repository.indexing_progress"]);

function LiveProgress({repositoryId}: {repositoryId: string}) {
  const {workspace} = useOps();
  const progress = useQuery<Progress | null>({queryKey: repositoryProgressKey(workspace?.id ?? "", repositoryId), queryFn: () => null, enabled: false, staleTime: Infinity});
  const value = progress.data;
  if (!value) return null;
  if (value.type === "repository.indexing_failed") return <p className="error" role="alert">Indexing failed{value.failure_code ? `: ${value.failure_code}` : ""}.</p>;
  if (!liveTypes.has(value.type)) return null;
  const percent = value.total_files ? Math.min(100, Math.round(((value.files_processed ?? 0) / value.total_files) * 100)) : null;
  return <div className="live-progress" role="status" aria-label="Live indexing progress"><strong>Indexing{value.stage ? `: ${value.stage}` : ""}</strong>
    {percent !== null && <progress max={100} value={percent}>{percent}%</progress>}
    <span>{value.total_files != null ? `${value.files_processed ?? 0}/${value.total_files} files` : "Preparing files"}{value.chunks_processed != null ? ` · ${value.chunks_processed} chunks` : ""}</span></div>;
}

function MappingSection({repository}: {repository: RepositoryDetail}) {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient(); const [error, setError] = useState("");
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});
  const refresh = async () => {await Promise.all(["repository", "repositories", "service-mappings", "suspect-changes"].map((name) => client.invalidateQueries({queryKey: [name, workspace?.id]})));};
  const create = useMutation({mutationFn: (input: {serviceId: string; environment: Environment; branch: string; prefix: string}) => api.createMapping(baseUrl, workspace!.id, input.serviceId, {repository_id: repository.id, environment: input.environment, branch_pattern: input.branch, path_prefix: input.prefix || null}), onSuccess: refresh, onError: (value: Error) => setError(value.message)});
  const remove = useMutation({mutationFn: (input: {serviceId: string; mappingId: string}) => api.deleteMapping(baseUrl, workspace!.id, input.serviceId, input.mappingId), onSuccess: refresh, onError: (value: Error) => setError(value.message)});
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); const form = event.currentTarget; const data = new FormData(form);
    const serviceId = String(data.get("service_id") ?? ""); const branch = String(data.get("branch_pattern") ?? "").trim();
    if (!serviceId) {setError("Choose a service."); return;}
    if (!branch || /[\\\s]/.test(branch) || branch.includes("..")) {setError("Enter a branch name or glob without spaces."); return;}
    const prefix = String(data.get("path_prefix") ?? "").trim();
    if (prefix && (prefix.includes("\\") || prefix.split("/").some((part) => part === ".." || part === "."))) {setError("Path prefix must be a relative repository path."); return;}
    create.mutate({serviceId, environment: String(data.get("environment")) as Environment, branch, prefix}, {onSuccess: () => form.reset()});
  }
  return <section className="panel"><h3>Connected services</h3>
    {repository.connected_services.length ? <ul className="mapping-list">{repository.connected_services.map((mapping) => <li key={mapping.mapping_id}><div><strong>{mapping.service_name}</strong><p className="muted">{mapping.environment} · branch <code>{mapping.branch_pattern}</code>{mapping.path_prefix ? <> · path <code>{mapping.path_prefix}</code></> : " · whole repository"}</p></div><button className="button danger" disabled={remove.isPending} aria-label={`Remove mapping for ${mapping.service_name}`} onClick={() => {if (window.confirm(`Remove the ${mapping.service_name} mapping? The repository and its indexed data are kept.`)) remove.mutate({serviceId: mapping.service_id, mappingId: mapping.mapping_id});}}>Remove</button></li>)}</ul> : <p className="empty">No services are mapped to this repository.</p>}
    <form className="form mapping-form" onSubmit={submit}><h4>Map a service</h4>
      <div className="form-grid"><label>Service<select name="service_id" defaultValue=""><option value="">Select a service</option>{services.data?.map((service) => <option key={service.id} value={service.id}>{service.name}</option>)}</select></label><label>Environment<select name="environment" defaultValue="production"><option value="development">development</option><option value="staging">staging</option><option value="production">production</option></select></label><label>Branch pattern<input name="branch_pattern" defaultValue="main"/></label><label>Monorepo path prefix (optional)<input name="path_prefix" placeholder="services/checkout/"/></label></div>
      {error && <p role="alert" className="error">{error}</p>}
      <button className="button primary" disabled={create.isPending}>{create.isPending ? "Mapping…" : "Create mapping"}</button></form></section>;
}

export function RepositoryDetailView({repositoryId}: {repositoryId: string}) {
  const {baseUrl, workspace, connection} = useOps(); const client = useQueryClient();
  const [notice, setNotice] = useState(""); const [actionError, setActionError] = useState("");
  const key = ["repository", workspace?.id, repositoryId];
  const repository = useQuery({queryKey: key, enabled: Boolean(workspace), queryFn: () => api.repository(baseUrl, workspace!.id, repositoryId), refetchInterval: (query) => connection === "degraded" && query.state.data?.status === "syncing" ? 3000 : false});
  const commits = useQuery({queryKey: ["repository-activity", workspace?.id, repositoryId, "commits"], enabled: Boolean(workspace), queryFn: () => api.repositoryCommits(baseUrl, workspace!.id, repositoryId, 10)});
  const deployments = useQuery({queryKey: ["repository-activity", workspace?.id, repositoryId, "deployments"], enabled: Boolean(workspace), queryFn: () => api.repositoryDeployments(baseUrl, workspace!.id, repositoryId, 10)});
  const refresh = async () => {await client.invalidateQueries({queryKey: key}); await client.invalidateQueries({queryKey: ["repositories", workspace?.id]});};
  const sync = useMutation({mutationFn: () => api.syncRepository(baseUrl, workspace!.id, repositoryId), onMutate: () => {setActionError(""); setNotice("");}, onSuccess: async () => {setNotice("Sync queued."); await refresh();}, onError: (value: Error) => setActionError(value.message)});
  const reindex = useMutation({mutationFn: () => api.reindexRepository(baseUrl, workspace!.id, repositoryId), onMutate: () => {setActionError(""); setNotice("");}, onSuccess: async () => {setNotice("Reindex queued."); await refresh();}, onError: (value: Error) => setActionError(value.message)});
  if (repository.isLoading) return <section className="panel" role="status">Loading repository…</section>;
  if (repository.isError || !repository.data) return <QueryFailure error={repository.error} what="repository"/>;
  const value = repository.data; const snapshot = value.active_snapshot; const busy = sync.isPending || reindex.isPending || value.status === "syncing";
  const unavailable = value.status === "removed" || value.status === "archived";
  return <div className="page-stack">
    <header className="detail-header"><div><p className="eyebrow">{value.is_private ? "Private repository" : "Public repository"}{value.is_archived ? " · archived on GitHub" : ""}</p><h2>{value.full_name}</h2><p className="muted">Default branch <code>{value.default_branch}</code></p></div><span className={`status-badge repo-${value.status}`}>{value.status}</span></header>
    {value.status === "failed" && <section className="failure-box" role="alert"><strong>Synchronization failed</strong><p>{value.failure_code ? `${value.failure_code}: ` : ""}{value.failure_message ?? "No further detail available."}</p></section>}
    <LiveProgress repositoryId={repositoryId}/>
    <section className="detail-grid"><div className="panel"><h3>Index status</h3><dl><dt>Indexed commit</dt><dd>{snapshot ? <code>{shortSha(snapshot.commit_sha)}</code> : "Not indexed"}</dd><dt>Branch</dt><dd>{snapshot?.branch ?? "—"}</dd><dt>Files</dt><dd>{snapshot?.file_count ?? 0}</dd><dt>Chunks</dt><dd>{snapshot?.chunk_count ?? 0}</dd><dt>Last indexed</dt><dd>{snapshot?.completed_at ? formatDate(snapshot.completed_at) : "—"}</dd><dt>Last synced</dt><dd>{value.last_synced_at ? formatDate(value.last_synced_at) : "Never"}</dd><dt>Commits / deployments</dt><dd>{value.commit_count} / {value.deployment_count}</dd></dl>
      <div className="control-group"><button className="button" disabled={busy || unavailable} onClick={() => sync.mutate()}>{sync.isPending ? "Queuing sync…" : "Sync"}</button><button className="button" disabled={busy || unavailable} onClick={() => reindex.mutate()}>{reindex.isPending ? "Queuing reindex…" : "Reindex"}</button></div>
      {value.status === "syncing" && <p className="muted" role="status">A synchronization is already running.</p>}
      {notice && <p role="status" className="notice">{notice}</p>}{actionError && <p role="alert" className="error">{actionError}</p>}</div>
      <div className="panel"><h3>Snapshot history</h3>{value.recent_snapshots.length ? <ol className="progress-list">{value.recent_snapshots.map((item) => <li key={item.id}><span className={`step-state ${item.status}`}>{item.status}</span><div><strong><code>{shortSha(item.commit_sha)}</code> · {item.branch}</strong><p>{item.file_count} files · {item.chunk_count} chunks · {formatDate(item.completed_at ?? item.created_at)}</p>{item.failure_message && <small className="error">{item.failure_code ? `${item.failure_code}: ` : ""}{item.failure_message}</small>}</div></li>)}</ol> : <p className="empty">No snapshots yet.</p>}</div></section>
    <MappingSection repository={value}/>
    <section className="detail-grid"><div className="panel"><h3>Recent commits</h3>{commits.isLoading ? <p role="status">Loading commits…</p> : commits.isError ? <QueryFailure error={commits.error} what="commits"/> : commits.data?.length ? <ul className="activity-list">{commits.data.map((commit) => <li key={commit.id}><code>{shortSha(commit.sha)}</code><div><strong>{commit.message.split("\n")[0]}</strong><p className="muted">{commit.author_name ?? "Unknown author"} · {commit.changed_files} files · +{commit.additions}/-{commit.deletions} · {formatDate(commit.committed_at)}</p></div></li>)}</ul> : <p className="empty">No commits synchronized.</p>}</div>
      <div className="panel"><h3>Recent deployments</h3>{deployments.isLoading ? <p role="status">Loading deployments…</p> : deployments.isError ? <QueryFailure error={deployments.error} what="deployments"/> : deployments.data?.length ? <ul className="activity-list">{deployments.data.map((deployment) => <li key={deployment.id}><code>{shortSha(deployment.commit_sha)}</code><div><strong>{deployment.environment} · {deployment.status}</strong><p className="muted">{deployment.description ?? "No description"} · {deployment.deployed_at ? formatDate(deployment.deployed_at) : "Not deployed"}</p></div></li>)}</ul> : <p className="empty">No deployments synchronized.</p>}</div></section>
  </div>;
}
