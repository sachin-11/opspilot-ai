"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, API_SCOPES } from "@/lib/api";
import { formatDate } from "./dashboard";
import { describeAuthError } from "./members-admin";
import { useOps } from "./providers";

export function ApiKeysAdmin() {
  const {baseUrl, workspace} = useOps();
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const [revealed, setRevealed] = useState<{name: string; key: string} | null>(null);
  const keys = useQuery({queryKey: ["api-keys", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.apiKeys(baseUrl, workspace!.id)});
  const refresh = () => client.invalidateQueries({queryKey: ["api-keys", workspace?.id]});
  const onError = async (error: Error) => { setMessage(describeAuthError(error)); await refresh(); };
  const create = useMutation({mutationFn: (v: {name: string; scopes: string[]}) => api.createApiKey(baseUrl, workspace!.id, v), onSuccess: async (value) => { setRevealed({name: value.name, key: value.key}); setMessage(""); await refresh(); }, onError});
  const rotate = useMutation({mutationFn: (id: string) => api.rotateApiKey(baseUrl, workspace!.id, id), onSuccess: async (value) => { setRevealed({name: value.name, key: value.key}); await refresh(); }, onError});
  const revoke = useMutation({mutationFn: (id: string) => api.revokeApiKey(baseUrl, workspace!.id, id), onSuccess: async () => { setMessage("API key revoked. It stops working immediately."); await refresh(); }, onError});

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const scopes = data.getAll("scope").map(String);
    if (!scopes.length) { setMessage("Select at least one scope."); return; }
    create.mutate({name: String(data.get("name") ?? "").trim(), scopes});
  }
  const ask = (text: string, action: () => void) => { if (window.confirm(text)) action(); };

  if (keys.isLoading) return <section className="panel" role="status">Loading API keys…</section>;
  if (keys.isError) return <section className="panel error" role="alert">{describeAuthError(keys.error)}</section>;
  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Machine access</p><h2>API keys</h2><p className="muted">Scoped, revocable keys for integrations. Keys can never approve or publish remediations, or manage members.</p></div></header>
    {message && <p role="alert" className="notice">{message}</p>}
    {revealed && <section className="panel secret-reveal" role="status"><strong>Key “{revealed.name}”</strong><p>Copy this key now. It is shown only once and only a hash is stored.</p><code className="secret-value">{revealed.key}</code><button className="button" onClick={() => setRevealed(null)}>I have copied it</button></section>}
    <section className="panel"><h3>Create key</h3>
      <form className="form-grid" onSubmit={submit}>
        <label>Name<input name="name" required maxLength={120}/></label>
        <fieldset><legend>Scopes</legend>{API_SCOPES.map((scope) => <label key={scope} className="check-row"><input type="checkbox" name="scope" value={scope}/> <code>{scope}</code></label>)}</fieldset>
        <button className="button primary" disabled={create.isPending}>Create API key</button>
      </form>
    </section>
    <section className="panel table-wrap"><table><caption className="sr-only">API keys</caption><thead><tr><th>Name</th><th>Prefix</th><th>Scopes</th><th>Status</th><th>Last used</th><th>Expires</th><th>Actions</th></tr></thead><tbody>
      {keys.data?.map((key) => <tr key={key.id}><td>{key.name}</td><td><code>{key.key_prefix}</code></td><td>{key.scopes.join(", ")}</td><td><span className="status-badge">{key.status}</span></td><td>{key.last_used_at ? formatDate(key.last_used_at) : "never"}</td><td>{key.expires_at ? formatDate(key.expires_at) : "—"}</td>
        <td>{key.status === "active" && <><button className="button" disabled={rotate.isPending} onClick={() => ask(`Rotate “${key.name}”? The current key stops working immediately.`, () => rotate.mutate(key.id))}>Rotate</button> <button className="button danger" disabled={revoke.isPending} onClick={() => ask(`Revoke “${key.name}”? This cannot be undone.`, () => revoke.mutate(key.id))}>Revoke</button></>}</td></tr>)}
    </tbody></table></section>
  </div>;
}
