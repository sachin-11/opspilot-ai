"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, ApiError, type Member, type Role } from "@/lib/api";
import { can, ROLE_LABELS } from "@/lib/permissions";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";

const ROLES: Role[] = ["owner", "admin", "responder", "analyst", "viewer"];

export function describeAuthError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "You do not have permission to do this. Your role may have changed.";
    if (error.status === 404) return "That item no longer exists in this workspace.";
    if (error.status === 409) return `${error.message}.`;
    if (error.status === 429) return "Too many requests. Please wait a moment and try again.";
    return error.message;
  }
  return error instanceof Error ? error.message : "Request failed.";
}

export function MembersAdmin() {
  const ops = useOps();
  const {baseUrl, workspace, role, refreshMemberships} = ops;
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const [issued, setIssued] = useState<{email: string; token: string} | null>(null);
  const manage = can(ops, "members.manage");
  const members = useQuery({queryKey: ["members", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.members(baseUrl, workspace!.id)});
  const invitations = useQuery({queryKey: ["invitations", workspace?.id], enabled: Boolean(workspace && manage), queryFn: () => api.invitations(baseUrl, workspace!.id)});
  const refresh = async () => { await client.invalidateQueries({queryKey: ["members", workspace?.id]}); await client.invalidateQueries({queryKey: ["invitations", workspace?.id]}); await refreshMemberships(); };
  const onError = async (error: Error) => { setMessage(describeAuthError(error)); await refresh(); };
  const update = useMutation({mutationFn: (v: {id: string; role?: Role; status?: "active" | "suspended"}) => api.updateMember(baseUrl, workspace!.id, v.id, {role: v.role, status: v.status}), onSuccess: async () => { setMessage("Member updated."); await refresh(); }, onError});
  const remove = useMutation({mutationFn: (id: string) => api.removeMember(baseUrl, workspace!.id, id), onSuccess: async () => { setMessage("Member removed. Their access has ended."); await refresh(); }, onError});
  const invite = useMutation({mutationFn: (v: {email: string; role: Role}) => api.createInvitation(baseUrl, workspace!.id, v), onSuccess: async (value) => { setIssued({email: value.email, token: value.token}); setMessage(""); await refresh(); }, onError});
  const revoke = useMutation({mutationFn: (id: string) => api.revokeInvitation(baseUrl, workspace!.id, id), onSuccess: refresh, onError});

  const activeOwners = (members.data ?? []).filter((item) => item.role === "owner" && item.status === "active").length;
  const isFinalOwner = (item: Member) => item.role === "owner" && item.status === "active" && activeOwners <= 1;
  const ownerOnly = (item: Member) => item.role === "owner" && role !== "owner";

  function changeRole(item: Member, next: Role) {
    if (next === item.role) return;
    if (!window.confirm(`Change ${item.email ?? item.display_name ?? "this member"} from ${ROLE_LABELS[item.role]} to ${ROLE_LABELS[next]}?`)) return;
    update.mutate({id: item.id, role: next});
  }
  function removeMember(item: Member) {
    if (!window.confirm(`Remove ${item.email ?? item.display_name ?? "this member"} from the workspace? Their access ends immediately.`)) return;
    remove.mutate(item.id);
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    invite.mutate({email: String(data.get("email") ?? "").trim(), role: String(data.get("role")) as Role});
    event.currentTarget.reset();
  }

  if (members.isLoading) return <section className="panel" role="status">Loading members…</section>;
  if (members.isError) return <section className="panel error" role="alert">{describeAuthError(members.error)}</section>;
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Access control</p><h2>Members</h2><p className="muted">Roles apply to this workspace only. Changes take effect on the next request.</p></div></header>
    {message && <p role="alert" className="notice">{message}</p>}
    <section className="panel table-wrap"><table><caption className="sr-only">Workspace members</caption><thead><tr><th>Member</th><th>Role</th><th>Status</th><th>Joined</th><th>Actions</th></tr></thead><tbody>
      {members.data?.map((item) => <tr key={item.id}>
        <td>{item.display_name ?? item.email ?? item.user_id}<br/><small className="muted">{item.email}</small></td>
        <td>{manage ? <select aria-label={`Role for ${item.email ?? item.user_id}`} value={item.role} disabled={isFinalOwner(item) || ownerOnly(item) || update.isPending || item.status === "revoked"} onChange={(event) => changeRole(item, event.target.value as Role)}>{ROLES.filter((r) => r !== "owner" || role === "owner").map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}</select> : ROLE_LABELS[item.role]}</td>
        <td><span className="status-badge">{item.status}</span></td>
        <td>{formatDate(item.created_at)}</td>
        <td>{manage && item.status !== "revoked" ? <button className="button danger" disabled={isFinalOwner(item) || ownerOnly(item) || remove.isPending} title={isFinalOwner(item) ? "A workspace must keep at least one owner" : undefined} onClick={() => removeMember(item)}>Remove</button> : null}{isFinalOwner(item) && <small className="muted"> Final owner</small>}</td>
      </tr>)}
    </tbody></table></section>
    {manage && <>
      <section className="panel"><h3>Invite a member</h3>
        <form className="form-grid" onSubmit={submit}>
          <label>Email<input name="email" type="email" required autoComplete="off"/></label>
          <label>Role<select name="role" defaultValue="viewer">{ROLES.filter((r) => r !== "owner" || role === "owner").map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}</select></label>
          <button className="button primary" disabled={invite.isPending}>Create invitation</button>
        </form>
        {issued && <div className="secret-reveal" role="status"><strong>Invitation for {issued.email}</strong><p>Copy this token now. It is shown only once and cannot be recovered.</p><code className="secret-value">{issued.token}</code><button className="button" onClick={() => setIssued(null)}>I have copied it</button></div>}
      </section>
      <section className="panel"><h3>Invitations</h3>{invitations.data?.length ? <ul className="plain-list">{invitations.data.map((item) => <li key={item.id}>{item.email} · {ROLE_LABELS[item.role]} · {item.accepted_at ? "accepted" : item.revoked_at ? "revoked" : `expires ${formatDate(item.expires_at)}`}{!item.accepted_at && !item.revoked_at && <> <button className="link-button" onClick={() => revoke.mutate(item.id)}>Revoke</button></>}</li>)}</ul> : <p className="muted">No invitations.</p>}</section>
    </>}
  </div>;
}
