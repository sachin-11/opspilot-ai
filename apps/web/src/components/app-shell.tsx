"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError, setCsrfToken } from "@/lib/api";
import { can, NAV_PERMISSIONS, ROLE_LABELS } from "@/lib/permissions";
import { useOps } from "./providers";

const links = [["/", "Overview"], ["/incidents", "Incidents"], ["/incidents/new", "Create incident"], ["/remediations", "Remediations"], ["/knowledge", "Knowledge"], ["/knowledge/search", "Knowledge search"], ["/repositories", "Repositories"], ["/repositories/search", "Code search"], ["/integrations/github", "GitHub"], ["/observability", "Pipeline"], ["/observability/sources", "Obs sources"], ["/observability/signals", "Signals"], ["/observability/dead-letters", "Dead letters"], ["/llmops", "LLMOps"], ["/settings/services", "Services"], ["/settings/members", "Members"], ["/settings/api-keys", "API keys"], ["/settings/audit", "Audit log"]] as const;

export function AppShell({children}: {children: React.ReactNode}) {
  const path = usePathname();
  const ops = useOps();
  const {workspace, workspaces, connection, user, role, selectWorkspace} = ops;
  const [signingOut, setSigningOut] = useState(false);
  async function logout() {
    setSigningOut(true);
    const session = await fetch("/api/auth/session", {credentials: "same-origin"}).then((response) => response.ok ? response.json() as Promise<{csrfToken: string}> : null).catch(() => null);
    const result = await fetch("/api/auth/logout", {method: "POST", credentials: "same-origin", headers: session ? {"X-CSRF-Token": session.csrfToken} : {}})
      .then((response) => response.ok ? response.json() as Promise<{redirectTo?: string | null}> : null).catch(() => null);
    setCsrfToken(null);
    // Ending the identity provider session too is what makes the next sign-in ask for credentials.
    window.location.href = result?.redirectTo || "/login";
  }
  // Role-aware navigation is a convenience; routes are enforced server-side regardless.
  const visible = links.filter(([href]) => can(ops, NAV_PERMISSIONS[href] ?? "workspace.read"));
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="sidebar-header">
        <p className="eyebrow">OpsPilot AI</p><h1>Operations</h1>
        {workspaces.length > 1
          ? <label className="workspace-select">Workspace<select aria-label="Workspace" value={workspace?.id ?? ""} onChange={(event) => selectWorkspace(event.target.value)}>{workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          : <p className="workspace-name">{workspace?.name ?? "No workspace"}</p>}
        {user && <p className="muted current-user" aria-label="Signed in user">{user.display_name ?? user.email ?? "Signed in"}{role ? ` · ${ROLE_LABELS[role] ?? role}` : ""}</p>}
      </div>
      <nav aria-label="Primary navigation">{visible.map(([href, label]) => <Link key={href} href={href} className={path === href || (href === "/knowledge" && path.startsWith("/knowledge/") && path !== "/knowledge/search") || (href === "/repositories" && path.startsWith("/repositories/") && path !== "/repositories/search") || (href === "/remediations" && path.startsWith("/remediations/")) || (href === "/llmops" && path.startsWith("/llmops")) ? "active" : ""}>{label}</Link>)}</nav>
      <div className="sidebar-footer">
        <p className={`connection ${connection}`}><span aria-hidden="true">●</span> Realtime {connection}</p>
        <button className="button" onClick={() => void logout()} disabled={signingOut}>{signingOut ? "Signing out…" : "Sign out"}</button>
      </div>
    </aside>
    <main className="content">{workspace ? children : <EmptyWorkspace />}</main>
  </div>;
}

function EmptyWorkspace() {
  const {baseUrl, user, selectWorkspace, refreshMemberships} = useOps();
  const [error, setError] = useState("");
  const create = useMutation({
    mutationFn: (name: string) => api.createWorkspace(baseUrl, {name}),
    onSuccess: async (created) => { setError(""); await refreshMemberships(); selectWorkspace(created.id); },
    onError: (failure) => setError(failure instanceof ApiError ? failure.message : "Could not create the workspace. Try again."),
  });
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("name") ?? "").trim();
    if (name) create.mutate(name);
  }
  return <section className="panel form" aria-labelledby="no-workspace-title">
    <h2 id="no-workspace-title">No workspace yet</h2>
    <p>{user?.email ?? "This account"} is not a member of any workspace. Ask a workspace owner for an invitation, or create your own workspace and become its owner.</p>
    <form className="form" onSubmit={submit}>
      <label>Workspace name<input name="name" maxLength={120} required /></label>
      {error && <p role="alert" className="error">{error}</p>}
      <button className="button primary" disabled={create.isPending}>{create.isPending ? "Creating…" : "Create workspace"}</button>
    </form>
  </section>;
}
