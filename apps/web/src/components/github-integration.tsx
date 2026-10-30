"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { FormEvent, useState } from "react";
import { api, type Installation } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";
import { QueryFailure } from "./repository-common";

function permissionSummary(permissions: Installation["permissions"]) {
  const entries = Object.entries(permissions).map(([name, level]) => [name, String(level)] as const);
  const writable = entries.filter(([, level]) => level !== "read");
  return {entries, writable};
}

function InstallationCard({installation}: {installation: Installation}) {
  const {entries, writable} = permissionSummary(installation.permissions);
  return <article className="panel installation-card">
    <header className="section-heading"><div><h3>{installation.account_login}</h3><p className="muted">{installation.account_type} · installation {installation.provider_installation_id} · {installation.repository_selection} repositories</p></div><span className={`status-badge install-${installation.status}`}>{installation.status}</span></header>
    {installation.status === "suspended" && <p className="notice" role="alert">This installation is suspended on GitHub{installation.suspended_at ? ` since ${formatDate(installation.suspended_at)}` : ""}. Repositories will not sync until it is unsuspended.</p>}
    {installation.status === "removed" && <p className="notice" role="alert">This installation was removed from GitHub{installation.removed_at ? ` on ${formatDate(installation.removed_at)}` : ""}. Existing indexed data is kept read-only; reinstall the app to sync again.</p>}
    <dl><dt>Repositories</dt><dd>{installation.repository_count}</dd><dt>Last sync</dt><dd>{installation.last_synced_at ? formatDate(installation.last_synced_at) : "Never synchronized"}</dd><dt>Permissions</dt><dd>{entries.length ? <ul className="permission-list">{entries.map(([name, level]) => <li key={name}><code>{name}</code>: {level}</li>)}</ul> : "None reported"}</dd></dl>
    {writable.length ? <p className="notice" role="alert">Unexpected non-read permissions granted: {writable.map(([name]) => name).join(", ")}. OpsPilot only uses read access; consider reducing them in GitHub.</p> : <p className="muted">Read-only access. OpsPilot never writes to your repositories.</p>}
  </article>;
}

export function GitHubIntegration() {
  const {baseUrl, workspace} = useOps(); const client = useQueryClient(); const [error, setError] = useState(""); const [queued, setQueued] = useState("");
  const key = ["github-installations", workspace?.id];
  const installations = useQuery({queryKey: key, enabled: Boolean(workspace), queryFn: () => api.githubInstallations(baseUrl, workspace!.id)});
  const sync = useMutation({
    mutationFn: (id: number) => api.syncInstallation(baseUrl, workspace!.id, id),
    onSuccess: async () => {setQueued("Installation sync queued. Repositories will appear as the sync completes."); await client.invalidateQueries({queryKey: key}); await client.invalidateQueries({queryKey: ["repositories", workspace?.id]});},
    onError: (value: Error) => setError(value.message),
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setQueued("");
    const raw = String(new FormData(event.currentTarget).get("installation_id") ?? "").trim();
    const id = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(id) || id < 1) {setError("Enter a numeric GitHub installation id."); return;}
    sync.mutate(id);
  }
  const data = installations.data;
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Read-only integration</p><h2>GitHub</h2><p className="muted">Connect repositories so incidents can cite recent commits, deployments, and code. OpsPilot never writes to GitHub.</p></div><Link className="button" href="/repositories">Repositories</Link></header>
    {installations.isLoading ? <section className="panel" role="status">Loading GitHub installations…</section> : installations.isError ? <QueryFailure error={installations.error} what="GitHub installations"/> : data && <>
      <section className="panel provider-mode"><span>Provider mode <strong className={`status-badge mode-${data.provider}`}>{data.provider}</strong></span>{data.provider === "fixture" && <p className="muted">Fixture mode serves bundled sample repositories. No real GitHub calls are made.</p>}</section>
      {!data.provider_configured && <section className="panel notice" role="alert"><h3>GitHub is not configured</h3><p>{data.configuration_hint ?? "Configure the GitHub App settings on the API service and restart it."}</p><p className="muted">Secrets are set only in the API environment and are never shown here.</p></section>}
      <form className="panel form" onSubmit={submit}><h3>Sync an installation</h3><label>Installation id<input name="installation_id" inputMode="numeric" placeholder="e.g. 12345678"/></label>{error && <p role="alert" className="error">{error}</p>}{queued && <p role="status" className="notice">{queued}</p>}<button className="button primary" disabled={sync.isPending || !data.provider_configured}>{sync.isPending ? "Queuing…" : "Sync installation"}</button></form>
      {data.installations.length ? <section className="card-list">{data.installations.map((installation) => <InstallationCard key={installation.id} installation={installation}/>)}</section> : <section className="panel empty"><h3>No installations synchronized</h3><p>Enter a GitHub App installation id above to import its repositories.</p></section>}
    </>}
  </div>;
}
