"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { api, type ApprovalAction, type RemediationDetail, type RemediationVerification } from "@/lib/api";
import { describeError, NO_AUTO_MERGE, shortHash } from "./remediation-common";
import { useOps } from "./providers";

export function ReviewPanel({remediation, verification}: {remediation: RemediationDetail; verification: RemediationVerification | undefined}) {
  const {baseUrl, workspace} = useOps();
  const client = useQueryClient();
  const [confirmed, setConfirmed] = useState(false);
  const [comment, setComment] = useState("");
  const [error, setError] = useState("");
  const binding = remediation.approval_binding;
  const policy = remediation.policy_evaluations.at(-1);
  const state = verification?.comparison?.state ?? remediation.verification_state ?? "unknown";
  const revisionId = remediation.current_revision_id;
  const mutation = useMutation({
    mutationFn: (action: ApprovalAction) => api.reviewRemediation(baseUrl, workspace!.id, remediation.id, {action, comment: comment.trim(), revision_id: revisionId!, version: remediation.version}),
    onSuccess: async () => {
      setError(""); setComment(""); setConfirmed(false);
      await client.invalidateQueries({queryKey: ["remediation", workspace?.id, remediation.id]});
      await client.invalidateQueries({queryKey: ["remediations", workspace?.id]});
    },
    onError: async (failure: Error) => {
      setError(describeError(failure));
      await client.invalidateQueries({queryKey: ["remediation", workspace?.id, remediation.id]});
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const action = submitter?.value as ApprovalAction | undefined;
    if (!action || !revisionId) return;
    if (!comment.trim()) { setError("A comment is required for every review action."); return; }
    if (action === "approved" && !confirmed) { setError("Confirm that you reviewed this exact patch before approving."); return; }
    setError("");
    mutation.mutate(action);
  }

  if (!binding || !revisionId) return <section className="panel review-panel"><h3>Human review</h3><p role="status" className="muted">Waiting for verification evidence before review is possible.</p></section>;
  return <section className="panel review-panel" aria-labelledby="review-heading">
    <p className="eyebrow">Explicit human approval</p>
    <h3 id="review-heading">Review this exact artifact</h3>
    <dl className="binding-list">
      <dt>Patch hash</dt><dd><code>{binding.patch_hash}</code></dd>
      <dt>Base commit</dt><dd><code>{binding.base_commit_sha}</code></dd>
      <dt>Policy version</dt><dd><code>{binding.policy_version}</code> · {policy?.decision.replaceAll("_", " ") ?? "unknown"}</dd>
      <dt>Verification</dt><dd>{state === "passed" ? "Passed: every required check executed and passed" : state === "partial" ? "Partial: some checks did not run or pre-existing failures remain" : state} · <code>{shortHash(binding.verification_hash)}</code></dd>
      <dt>Changes</dt><dd>{remediation.files_changed} files · +{remediation.lines_added} / −{remediation.lines_deleted}</dd>
      <dt>Approval valid for</dt><dd>{Math.round(binding.expires_in_seconds / 60)} minutes after approval</dd>
    </dl>
    {policy && policy.warnings.length > 0 && <div className="notice" role="note"><strong>Risk warnings</strong><ul>{policy.warnings.map((warning) => <li key={`${warning.code}-${warning.path ?? ""}`}>{warning.message}{warning.path ? ` (${warning.path})` : ""}</li>)}</ul></div>}
    {state === "partial" && <p className="notice" role="note">Verification is partial. Review the skipped checks and pre-existing failures above before approving.</p>}
    <p className="notice" role="note"><strong>Approving creates a GitHub fix branch and pull request</strong> for exactly this patch. {NO_AUTO_MERGE}</p>
    <form className="review-form" onSubmit={submit}>
      <label className="check-row"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}/> I reviewed this exact patch (hash <code>{shortHash(binding.patch_hash)}</code>), its policy result, and its verification evidence.</label>
      <label>Comment (required)<textarea value={comment} onChange={(event) => setComment(event.target.value)} rows={3} maxLength={2000}/></label>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="review-actions">
        <button className="button primary" name="action" value="approved" disabled={mutation.isPending || !confirmed}>Approve and create pull request</button>
        <button className="button" name="action" value="revision_requested" disabled={mutation.isPending}>Request revision</button>
        <button className="button danger" name="action" value="rejected" disabled={mutation.isPending}>Reject</button>
      </div>
    </form>
  </section>;
}
