"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";
import { ApiError, api, type InvestigationDetail, type ReviewAction } from "@/lib/api";
import { formatDate } from "./dashboard";
import { useOps } from "./providers";

const terminal = new Set(["completed", "rejected", "failed", "cancelled"]);

export function InvestigationPanel({incidentId}: {incidentId: string}) {
  const {baseUrl, workspace, connection} = useOps();
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const listKey = ["investigations", workspace?.id, incidentId];
  const list = useQuery({queryKey: listKey, enabled: Boolean(workspace), queryFn: () => api.investigations(baseUrl, workspace!.id, incidentId), refetchInterval: connection === "degraded" ? 5000 : false});
  const current = list.data?.[0] ?? null;
  const detailKey = ["investigation", workspace?.id, current?.id];
  const detail = useQuery({
    queryKey: detailKey,
    enabled: Boolean(workspace && current),
    queryFn: () => api.investigation(baseUrl, workspace!.id, current!.id),
    refetchInterval: current && connection === "degraded" && !terminal.has(current.status) ? 3000 : false,
  });
  const invalidate = async (investigationId?: string) => {
    await client.invalidateQueries({queryKey: listKey});
    if (investigationId) await client.invalidateQueries({queryKey: ["investigation", workspace?.id, investigationId]});
  };
  const onError = async (error: Error) => {
    setMessage(error instanceof ApiError && error.status === 409 ? "Investigation changed elsewhere. Latest state has been loaded." : error.message);
    await invalidate(current?.id);
  };
  const start = useMutation({mutationFn: () => api.startInvestigation(baseUrl, workspace!.id, incidentId), onSuccess: async (value) => {setMessage("Investigation queued."); await invalidate(value.investigation_id);}, onError});
  const cancel = useMutation({mutationFn: () => api.cancelInvestigation(baseUrl, workspace!.id, current!.id), onSuccess: () => invalidate(current!.id), onError});
  const retry = useMutation({mutationFn: () => api.retryInvestigation(baseUrl, workspace!.id, current!.id), onSuccess: async (value) => {setMessage("Retry queued as a new investigation."); await invalidate(value.investigation_id);}, onError});
  const review = useMutation({mutationFn: ({action, comment}: {action: ReviewAction; comment?: string}) => api.reviewInvestigation(baseUrl, workspace!.id, current!.id, action, comment, detail.data!.version), onSuccess: () => invalidate(current!.id), onError});
  const evidence = useMemo(() => new Map(detail.data?.evidence.map((item) => [item.id, item]) ?? []), [detail.data?.evidence]);

  if (list.isLoading) return <section className="panel investigation-panel"><h3>AI investigation</h3><p role="status">Loading investigations…</p></section>;
  if (list.isError) return <section className="panel investigation-panel"><h3>AI investigation</h3><p role="alert" className="error">Investigations could not be loaded.</p></section>;
  if (!current) return <section className="panel investigation-panel"><div className="section-heading"><div><p className="eyebrow">Human-reviewed assistance</p><h3>AI investigation</h3></div><button className="button primary" disabled={start.isPending} onClick={() => start.mutate()}>{start.isPending ? "Queuing…" : "Start investigation"}</button></div><p className="muted">Collect bounded evidence and prepare an advisory report. No production changes are executed.</p>{message && <p role="alert" className="notice">{message}</p>}</section>;
  const investigation = detail.data;
  return <section className="panel investigation-panel">
    <div className="section-heading"><div><p className="eyebrow">Human-reviewed assistance</p><h3>{investigation?.title ?? "AI investigation"}</h3></div><span className="status-badge">{current.status.replaceAll("_", " ")}</span></div>
    <p className="muted">{current.primary_provider} / {current.primary_model} · prompt {current.prompt_version}</p>
    {message && <p role="alert" className="notice">{message}</p>}
    {detail.isLoading ? <p role="status">Loading investigation progress…</p> : detail.isError || !investigation ? <p role="alert" className="error">Investigation detail unavailable.</p> : <>
      <div className="investigation-metrics"><span>Evidence <strong>{investigation.evidence.length}</strong></span><span>Input tokens <strong>{investigation.total_input_tokens}</strong></span><span>Output tokens <strong>{investigation.total_output_tokens}</strong></span><span>Estimated cost <strong>{investigation.estimated_cost_usd == null ? "Unavailable" : `$${investigation.estimated_cost_usd.toFixed(4)}`}</strong></span></div>
      <ol className="progress-list">{investigation.steps.map((step) => <li key={`${step.id}-${step.attempt}`}><span className={`step-state ${step.status}`}>{step.status}</span><div><strong>{step.display_name}</strong><p>{step.summary ?? "In progress"}</p><small>{step.completed_at ? `${Math.max(0, (new Date(step.completed_at).getTime() - new Date(step.started_at).getTime()) / 1000).toFixed(1)}s` : `Started ${formatDate(step.started_at)}`}</small></div></li>)}</ol>
      {investigation.status === "failed" && <div className="failure-box"><strong>{investigation.failure_code}</strong><p>{investigation.failure_message}</p><button className="button" disabled={retry.isPending} onClick={() => retry.mutate()}>Retry as new investigation</button></div>}
      {["queued", "running", "awaiting_review"].includes(investigation.status) && <button className="button danger" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancel investigation</button>}
      {investigation.report && <InvestigationReportView investigation={investigation} evidence={evidence} />}
      {investigation.status === "awaiting_review" && <ReviewForm pending={review.isPending} onSubmit={(action, comment) => review.mutate({action, comment})} />}
    </>}
  </section>;
}

function InvestigationReportView({investigation, evidence}: {investigation: InvestigationDetail; evidence: Map<string, InvestigationDetail["evidence"][number]>}) {
  const report = investigation.report!;
  const steps = (title: string, values: typeof report.remediation_plan) => <section><h4>{title}</h4><ol>{values.map((item) => <li key={`${title}-${item.order}`}><strong>{item.action}</strong><p>{item.rationale}</p><Citations ids={item.evidence_ids} evidence={evidence}/></li>)}</ol></section>;
  return <article className="report"><h3>Investigation report</h3>{report.abstained && <p className="notice">The investigation abstained because evidence was insufficient.</p>}<h4>Executive summary</h4><p>{report.executive_summary}</p><h4>Most likely root cause · {Math.round(report.confidence * 100)}% confidence</h4><p>{report.root_cause}</p><Citations ids={report.evidence_citations} evidence={evidence}/><h4>Impact</h4><p>{report.impact}</p><h4>Alternative hypotheses</h4><ul>{report.alternative_hypotheses.map((item) => <li key={item}>{item}</li>)}</ul>{steps("Immediate containment", report.immediate_containment_steps)}{steps("Remediation plan", report.remediation_plan)}{steps("Verification plan", report.verification_plan)}<h4>Unknowns and assumptions</h4><ul>{report.unknowns_and_assumptions.map((item) => <li key={item}>{item}</li>)}</ul><p className="advisory">Advisory only. OpsPilot does not execute remediation.</p></article>;
}

function Citations({ids, evidence}: {ids: string[]; evidence: Map<string, InvestigationDetail["evidence"][number]>}) {
  return <div className="citations">{ids.map((id) => {const item = evidence.get(id); return item ? <details key={id}><summary>{item.title}</summary><p>{item.excerpt}</p><small>{item.evidence_type} · {formatDate(item.collected_at)}</small></details> : null;})}</div>;
}

function ReviewForm({pending, onSubmit}: {pending: boolean; onSubmit: (action: ReviewAction, comment?: string) => void}) {
  const [error, setError] = useState("");
  function submit(event: FormEvent<HTMLFormElement>) {event.preventDefault(); const data = new FormData(event.currentTarget); const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null; const action = submitter?.value as ReviewAction; const comment = String(data.get("comment") ?? "").trim(); if ((action === "revision_requested" || action === "rejected") && !comment) {setError("Feedback is required for revision or rejection."); return;} if ((action === "approved" || action === "rejected") && !window.confirm(`Confirm ${action.replace("_", " ")}?`)) return; setError(""); onSubmit(action, comment || undefined);}
  return <form className="review-form" onSubmit={submit}><h4>Human review</h4><label>Feedback<textarea name="comment" rows={3} maxLength={2000}/></label>{error && <p role="alert" className="error">{error}</p>}<div className="review-actions"><button className="button primary" name="action" value="approved" disabled={pending}>Approve report</button><button className="button" name="action" value="revision_requested" disabled={pending}>Request revision</button><button className="button danger" name="action" value="rejected" disabled={pending}>Reject report</button></div></form>;
}
