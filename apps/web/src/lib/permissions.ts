/** UX-only permission helpers. The API remains the sole authority; hidden buttons are not security. */
export type PermissionHolder = {permissions?: string[]};

/** ``permissions`` is absent only when a component is rendered without the workspace provider. */
export function can(holder: PermissionHolder, permission: string): boolean {
  return holder.permissions === undefined ? true : holder.permissions.includes(permission);
}

export const NAV_PERMISSIONS: Record<string, string> = {
  "/incidents": "incidents.read",
  "/incidents/new": "incidents.manage",
  "/remediations": "remediations.read",
  "/knowledge": "knowledge.read",
  "/knowledge/search": "knowledge.read",
  "/repositories": "repositories.read",
  "/repositories/search": "repositories.read",
  "/integrations/github": "integrations.read",
  "/observability": "observability.read",
  "/observability/sources": "observability.read",
  "/observability/signals": "observability.read",
  "/observability/dead-letters": "observability.read",
  "/llmops": "llmops.read",
  "/settings/services": "services.read",
  "/settings/members": "members.manage",
  "/settings/api-keys": "api_keys.manage",
  "/settings/audit": "audit.read",
};

export const ROLE_LABELS: Record<string, string> = {
  owner: "Owner", admin: "Admin", responder: "Responder", analyst: "Analyst", viewer: "Viewer",
};
