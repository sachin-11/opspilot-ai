"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useOps } from "./providers";

const links = [["/", "Overview"], ["/incidents", "Incidents"], ["/incidents/new", "Create incident"], ["/knowledge", "Knowledge"], ["/knowledge/search", "Knowledge search"], ["/repositories", "Repositories"], ["/repositories/search", "Code search"], ["/integrations/github", "GitHub"],["/settings/services", "Services"]] as const;

export function AppShell({children}: {children: React.ReactNode}) {
  const path = usePathname();
  const {workspace, connection} = useOps();
  return <div className="app-shell">
    <aside className="sidebar">
      <div><p className="eyebrow">OpsPilot AI</p><h1>Operations</h1><p className="workspace-name">{workspace?.name ?? "No workspace"}</p></div>
      <nav aria-label="Primary navigation">{links.map(([href, label]) => <Link key={href} href={href} className={path === href || (href === "/knowledge" && path.startsWith("/knowledge/") && path !== "/knowledge/search") || (href === "/repositories" && path.startsWith("/repositories/") && path !== "/repositories/search") ? "active" : ""}>{label}</Link>)}</nav>
      <p className={`connection ${connection}`}><span aria-hidden="true">●</span> Realtime {connection}</p>
    </aside>
    <main className="content">{workspace ? children : <EmptyWorkspace />}</main>
  </div>;
}

function EmptyWorkspace() {
  const {baseUrl} = useOps();
  return <section className="panel"><h2>No workspace yet</h2><p>Create one through the API or run the demo seed.</p><code>docker compose exec api python -m opspilot_api.incidents.seed</code><p className="muted">API: {baseUrl}</p></section>;
}
