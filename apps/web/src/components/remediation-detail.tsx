"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type RemediationDetail, type RemediationVerification, type SandboxRun } from "@/lib/api";
import { formatDate } from "./dashboard";
import { DiffViewer, type BlockedFile } from "./diff-viewer";
import { can } from "@/lib/permissions";
import { useOps } from "./providers";
import { ACTIVE_STATUSES, describeError, IN_FLIGHT_STATUSES, NO_AUTO_MERGE, RETRYABLE_STATUSES, RemediationStatusBadge, shortHash } from "./remediation-common";
import { ReviewPanel } from "./remediation-review";

export function RemediationDetailView({remediationId}: {remediationId: string}) {
  const ops = useOps();
  const {baseUrl, workspace, connection} = ops;
  const client = useQueryClient();
  const [message, setMessage] = useState("");
  const key = ["remediation", workspace?.id, remediationId];
  const detail = useQuery({queryKey: key, enabled: Boolean(workspace), queryFn: () => api.remediation(baseUrl, workspace!.id, remediationId), refetchInterval: (query) => connection === "degraded" && query.state.data && IN_FLIGHT_STATUSES.has(query.state.data.status) ? 3000 : false});
  const verification = useQuery({queryKey: ["remediation-verification", workspace?.id, remediationId], enabled: Boolean(workspace && detail.data && detail.data.current_revision_id), queryFn: () => api.remediationVerification(baseUrl, workspace!.id, remediationId)});
  const refresh = async () => { await client.invalidateQueries({queryKey: key}); await client.invalidateQueries({queryKey: ["remediation-verification", workspace?.id, remediationId]}); };
  const onError = async (error: Error) => { setMessage(describeError(error)); await refresh(); };
  const cancel = useMutation({mutationFn: () => api.cancelRemediation(baseUrl, workspace!.id, remediationId), onSuccess: refresh, onError});
  const retry = useMutation({mutationFn: () => api.retryRemediation(baseUrl, workspace!.id, remediationId), onSuccess: (value) => setMessage(`Retry queued as a new remediation (${value.remediation_id.slice(0, 8)}).`), onError});
  const publish = useMutation({mutationFn: () => api.publishRemediation(baseUrl, workspace!.id, remediationId), onSuccess: async () => { setMessage("Publication queued."); await refresh(); }, onError});

  if (detail.isLoading) return <section className="panel" role="status">Loading remediation…</section>;
  if (detail.isError || !detail.data) return <section className="panel error" role="alert">Remediation could not be loaded.</section>;
  const item = detail.data;
  const policy = item.policy_evaluations.at(-1);
  const currentRevision = item.revisions.find((revision) => revision.id === item.current_revision_id) ?? item.revisions.at(-1);
  const blocked: BlockedFile[] = (policy?.violations ?? []).filter((violation) => violation.path).reduce<BlockedFile[]>((acc, violation) => {
    const existing = acc.find((entry) => entry.path === violation.path);
    if (existing) existing.reasons.push(violation.code); else acc.push({path: violation.path!, reasons: [violation.code]});
    return acc;
  }, []);
  const invalidated = invalidatedApproval(item);
  const abstained = item.failure_code?.startsWith("abstained_") ?? false;

  return <div className="page-stack remediation-detail">
    <header className="page-header"><div><p className="eyebrow">Evidence-backed remediation</p><h2>{item.title}</h2><p className="muted">{item.repository_full_name ?? "Repository"} · base <code>{item.base_commit_sha.slice(0, 12)}</code> · version {item.version}</p></div><div className="control-group"><RemediationStatusBadge status={item.status}/><span className={`status-badge risk-${item.risk_level}`}>Risk: {item.risk_level}</span></div></header>
    {message && <p role="alert" className="notice">{message}</p>}
    <p className="notice no-auto-merge" role="note">{NO_AUTO_MERGE}</p>
    <div className="control-group">
      {ACTIVE_STATUSES.has(item.status) && item.status !== "publishing" && <button className="button danger" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancel remediation</button>}
      {RETRYABLE_STATUSES.has(item.status) && <button className="button" disabled={retry.isPending} onClick={() => retry.mutate()}>Regenerate against the latest base</button>}
      <Link className="button" href={`/incidents/${item.incident_id}`}>Open incident</Link>
    </div>

    {invalidated && <section className="panel failure-box" role="alert"><strong>Approval invalidated.</strong><p>{invalidated} A fresh review of the re-verified artifact is required before anything can be published.</p></section>}
    {item.status === "stale" && <section className="panel failure-box" role="alert"><strong>Stale base commit.</strong><p>{item.stale_reason ?? "The default branch advanced."} The previous approval is void. Regenerate the remediation against the new base commit; OpsPilot never rebases automatically.</p></section>}
    {item.status === "policy_blocked" && <section className="panel failure-box" role="alert"><strong>Blocked by deterministic policy.</strong><p>This patch cannot be verified, approved, or published. A model cannot override these rules.</p></section>}
    {item.status === "verification_failed" && <section className="panel failure-box" role="alert"><strong>Verification failed.</strong><p>This patch cannot reach approval. See the verification results below.</p></section>}
    {item.status === "failed" && <section className="panel failure-box" role="alert"><strong>{abstained ? "No safe automated patch" : item.failure_code}</strong><p>{item.failure_message}</p></section>}

    <section className="panel"><h3>Problem and proposed solution</h3><p>{item.problem_statement || "No patch has been drafted yet."}</p><h4>Root cause</h4><p>{item.root_cause ?? "—"}</p><h4>Proposed solution</h4><p className="pre-wrap">{item.proposed_solution || "—"}</p>
      <h4>Supporting evidence</h4>{item.evidence.length ? <ul className="plain-list">{item.evidence.map((evidence) => <li key={evidence.id}><details><summary>{evidence.title}</summary><p>{evidence.excerpt}</p><small>{evidence.source_type}</small></details></li>)}</ul> : <p className="muted">No cited evidence yet.</p>}
    </section>

    {policy && <PolicyPanel evaluation={policy}/>}

    {currentRevision && <section className="panel"><div className="section-heading"><div><h3>Patch</h3><p className="muted">{item.files_changed} files · +{item.lines_added} / −{item.lines_deleted} · revision {currentRevision.revision_number}</p></div></div><dl className="binding-list"><dt>Patch hash</dt><dd><code>{currentRevision.patch_hash}</code></dd><dt>Base commit</dt><dd><code>{currentRevision.base_commit_sha}</code></dd></dl><DiffViewer patch={currentRevision.patch_text} blocked={blocked}/></section>}

    <VerificationPanel verification={verification.data} loading={verification.isLoading}/>

    <section className="panel"><h3>Model usage</h3><p className="muted">{item.generator_provider} / {item.generator_model} · prompt {item.generator_prompt_version}</p><div className="investigation-metrics"><span>Input tokens <strong>{item.total_input_tokens}</strong></span><span>Output tokens <strong>{item.total_output_tokens}</strong></span><span>Estimated cost <strong>{item.estimated_cost_usd == null ? "Unavailable" : `$${item.estimated_cost_usd.toFixed(4)}`}</strong></span></div></section>

    {item.status === "awaiting_approval" && <ReviewPanel remediation={item} verification={verification.data}/>}
    {item.status === "approved" && <section className="panel"><h3>Approved. Ready to publish</h3><p>Publishing creates one GitHub fix branch, one commit, and one pull request for patch <code>{shortHash(item.patch_hash)}</code>. {NO_AUTO_MERGE}</p>{!item.write_enabled && <p className="notice" role="alert">GitHub write access is disabled for this workspace and repository, so publication is unavailable.</p>}<button className="button primary" disabled={publish.isPending || !item.write_enabled || !can(ops, "remediations.publish")} title={can(ops, "remediations.publish") ? undefined : "Requires the remediations.publish permission"} onClick={() => publish.mutate()}>Create branch and pull request</button></section>}
    {item.status === "publishing" && <section className="panel" role="status"><h3>Publishing…</h3><p>Creating the fix branch, commit, and pull request. This page updates automatically.</p><progress aria-label="Publication progress"/></section>}
    {item.pull_request && <PullRequestPanel remediation={item}/>}

    <section className="panel"><h3>Revision history</h3><ol className="plain-list">{item.revisions.map((revision) => <li key={revision.id}><strong>Revision {revision.revision_number}</strong> {revision.id === item.current_revision_id && <span className="status-badge">current</span>}<p className="muted">{revision.change_summary}</p><small><code>{shortHash(revision.patch_hash)}</code> · {revision.files_changed} files · {formatDate(revision.created_at)}</small></li>)}{!item.revisions.length && <li className="muted">No revisions.</li>}</ol></section>
    <section className="panel"><h3>Approval history</h3><ol className="plain-list">{item.approvals.map((approval) => <li key={approval.id}><strong>{approval.action.replaceAll("_", " ")}</strong> <small>{formatDate(approval.created_at)} · {approval.actor_id ?? "actor recorded after authentication (Module 7)"}</small><p>{approval.comment}</p>{approval.action === "approved" && <small>Bound to <code>{shortHash(approval.approved_patch_hash)}</code> @ <code>{shortHash(approval.approved_base_commit_sha)}</code>{approval.expires_at ? ` · expires ${formatDate(approval.expires_at)}` : ""}</small>}</li>)}{!item.approvals.length && <li className="muted">No review decisions yet.</li>}</ol></section>
    <section className="panel"><h3>Workflow</h3><ol className="progress-list">{item.steps.map((step) => <li key={step.id}><span className={`step-state ${step.status}`}>{step.status}</span><div><strong>{step.display_name}</strong><p>{step.summary ?? "In progress"}</p></div></li>)}</ol></section>
  </div>;
}

function invalidatedApproval(item: RemediationDetail): string | null {
  const events = item.audit_events;
  const lastInvalidation = events.findLast((event) => event.action === "approval_invalidated");
  if (!lastInvalidation || ["approved", "publishing", "pull_request_created"].includes(item.status)) return null;
  const lastApproval = events.findLast((event) => event.action === "review_approved");
  if (lastApproval && lastApproval.created_at > lastInvalidation.created_at) return null;
  return "A bound value (patch, base commit, policy version, verification result, or expiry) changed after approval.";
}

function PolicyPanel({evaluation}: {evaluation: RemediationDetail["policy_evaluations"][number]}) {
  return <section className="panel policy-panel"><div className="section-heading"><h3>Policy decision</h3><span className={`status-badge ${evaluation.decision === "blocked" ? "repo-failed" : evaluation.decision === "allowed" ? "repo-active" : "repo-syncing"}`}>{evaluation.decision.replaceAll("_", " ")}</span></div><p className="muted">Deterministic policy <code>{evaluation.policy_version}</code> · risk {evaluation.risk_level}. No model participates in this decision.</p>
    {evaluation.violations.length > 0 && <div role="alert"><h4>Violations</h4><ul>{evaluation.violations.map((violation, index) => <li key={index} className="error"><code>{violation.code}</code> {violation.message}{violation.path ? ` · ${violation.path}` : ""}{violation.line ? `:${violation.line}` : ""}</li>)}</ul></div>}
    {evaluation.warnings.length > 0 && <div><h4>Warnings</h4><ul>{evaluation.warnings.map((warning, index) => <li key={index}><code>{warning.code}</code> {warning.message}{warning.path ? ` · ${warning.path}` : ""}</li>)}</ul></div>}
  </section>;
}

function runChecks(run: SandboxRun | null | undefined) { return new Map((run?.checks ?? []).map((check) => [check.command_id, check])); }

function VerificationPanel({verification, loading}: {verification: RemediationVerification | undefined; loading: boolean}) {
  if (loading) return <section className="panel" role="status">Loading verification…</section>;
  if (!verification || (!verification.baseline && !verification.patched)) return <section className="panel"><h3>Verification</h3><p className="muted">No sandbox verification has run yet.</p></section>;
  const {baseline, patched, comparison} = verification;
  const base = runChecks(baseline);
  const after = runChecks(patched);
  const commands = [...new Set([...base.keys(), ...after.keys()])].filter((id) => id !== "patch.apply");
  const label = comparison?.state === "passed" ? "Passed: every required check executed and passed" : comparison?.state === "partial" ? "Partial verification" : comparison?.state === "not_run" ? "Not verified: no test check executed" : "Failed";
  return <section className="panel verification-panel"><div className="section-heading"><h3>Verification</h3><span className={`status-badge ${comparison?.state === "passed" ? "repo-active" : comparison?.state === "partial" ? "repo-syncing" : "repo-failed"}`}>{label}</span></div>
    <div className="table-wrap"><table><caption className="sr-only">Baseline versus patched checks</caption><thead><tr><th>Check</th><th>Baseline (before patch)</th><th>Patched</th></tr></thead><tbody>
      {patched && <tr><td>Apply patch</td><td>—</td><td>{after.get("patch.apply")?.status ?? "—"}</td></tr>}
      {commands.map((id) => <tr key={id}><td>{after.get(id)?.display_name ?? base.get(id)?.display_name ?? id}</td><td>{base.get(id)?.status ?? "not run"}</td><td>{after.get(id)?.status ?? "not run"}</td></tr>)}
    </tbody></table></div>
    {comparison && <div className="verification-lists">
      {comparison.new_failures.length > 0 && <div role="alert" className="failure-box"><h4>New failures introduced by the patch</h4><ul>{comparison.new_failures.flatMap((entry) => entry.tests.map((test) => <li key={`${entry.command_id}-${test}`}><code>{test}</code></li>))}</ul></div>}
      {comparison.existing_failures.length > 0 && <div className="notice"><h4>Existing failures (present before the patch)</h4><ul>{comparison.existing_failures.flatMap((entry) => entry.tests.map((test) => <li key={`${entry.command_id}-${test}`}><code>{test}</code></li>))}</ul></div>}
      {comparison.fixed.length > 0 && <div><h4>Fixed by the patch</h4><ul>{comparison.fixed.map((test) => <li key={test}><code>{test}</code></li>)}</ul></div>}
      {comparison.skipped.length > 0 && <div className="notice"><h4>Checks that did not run</h4><ul>{comparison.skipped.map((entry) => <li key={entry.command_id}><code>{entry.command_id}</code>: {entry.reason}</li>)}</ul></div>}
      {comparison.notes.map((note) => <p key={note} className="muted">{note}</p>)}
    </div>}
    {patched && <details><summary>Sandbox resource limits</summary><dl className="binding-list"><dt>Provider</dt><dd>{patched.sandbox_provider}</dd><dt>CPU</dt><dd>{patched.cpu_limit}</dd><dt>Memory</dt><dd>{patched.memory_limit_mb} MB</dd><dt>Disk</dt><dd>{patched.disk_limit_mb} MB</dd><dt>Timeout</dt><dd>{patched.timeout_seconds}s</dd><dt>Network</dt><dd>{patched.network_enabled ? "enabled" : "disabled"}</dd><dt>Cleanup verified</dt><dd>{patched.cleanup_verified ? "yes" : "no"}</dd></dl></details>}
  </section>;
}

function PullRequestPanel({remediation}: {remediation: RemediationDetail}) {
  const pr = remediation.pull_request!;
  if (pr.status === "created" && pr.pull_request_url) return <section className="panel" role="status"><h3>Pull request created</h3><p><a href={pr.pull_request_url} target="_blank" rel="noreferrer noopener">#{pr.pull_request_number} on branch <code>{pr.branch_name}</code></a></p><p className="muted">Commit <code>{shortHash(pr.commit_sha)}</code> · patch <code>{shortHash(pr.patch_hash)}</code>. {NO_AUTO_MERGE}</p></section>;
  if (pr.status === "failed") return <section className="panel failure-box" role="alert"><h3>Publication failed</h3><p><code>{pr.failure_code}</code> {pr.failure_message}</p><p className="muted">No merge or deployment was attempted.</p></section>;
  return <section className="panel" role="status"><h3>Pull request {pr.status}</h3><p className="muted">Branch <code>{pr.branch_name}</code></p></section>;
}
