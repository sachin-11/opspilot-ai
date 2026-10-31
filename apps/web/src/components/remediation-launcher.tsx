"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { useOps } from "./providers";
import { describeError, RemediationStatusBadge } from "./remediation-common";

const FINISHED = ["policy_blocked", "verification_failed", "pull_request_created", "rejected", "stale", "failed", "cancelled"];

/** Incident-page entry point: a fix can only be proposed from a completed investigation. */
export function RemediationLauncher({incidentId}: {incidentId: string}) {
  const {baseUrl, workspace} = useOps();
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const investigations = useQuery({queryKey: ["investigations", workspace?.id, incidentId], enabled: Boolean(workspace), queryFn: () => api.investigations(baseUrl, workspace!.id, incidentId)});
  const completed = investigations.data?.find((item) => item.status === "completed");
  const remediations = useQuery({queryKey: ["remediations", workspace?.id, "investigation", completed?.id], enabled: Boolean(workspace && completed), queryFn: () => api.remediations(baseUrl, workspace!.id, new URLSearchParams({investigation_id: completed!.id}))});
  const start = useMutation({
    mutationFn: () => api.startRemediation(baseUrl, workspace!.id, completed!.id),
    onSuccess: async () => { setMessage("Remediation queued. Open it to follow policy and sandbox verification."); await client.invalidateQueries({queryKey: ["remediations", workspace?.id]}); },
    onError: (error: Error) => setMessage(describeError(error)),
  });
  if (!completed) return null;
  const active = remediations.data?.some((item) => !FINISHED.includes(item.status));
  return <section className="panel remediation-launcher">
    <div className="section-heading"><div><p className="eyebrow">Human-approved changes only</p><h3>Remediation</h3></div><button className="button primary" disabled={start.isPending || Boolean(active)} onClick={() => start.mutate()}>{start.isPending ? "Queuing…" : "Propose fix"}</button></div>
    <p className="muted">Drafts a bounded patch from the approved investigation, checks it against deterministic policy, verifies it in an isolated sandbox, and only after your explicit approval opens a GitHub pull request. It never merges or deploys.</p>
    {message && <p role="alert" className="notice">{message}</p>}
    {remediations.data?.length ? <ul className="plain-list">{remediations.data.map((item) => <li key={item.id}><Link href={`/remediations/${item.id}`}>{item.title}</Link> <RemediationStatusBadge status={item.status}/></li>)}</ul> : null}
  </section>;
}
