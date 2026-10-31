"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type PromptDetail, type PromptVersion } from "@/lib/api";
import { can } from "@/lib/permissions";
import { diffLines, diffStats } from "@/lib/text-diff";
import { Async, ConfirmDialog, fmtDate, LlmopsHeader, Notice, humanize } from "./llmops-common";
import { EvalComparison, GateReportView, gateSummary } from "./llmops-eval-parts";
import { useOps } from "./providers";

const hashPrefix = (hash: string) => hash.slice(0, 12);
const byNewest = (a: PromptVersion, b: PromptVersion) => b.created_at.localeCompare(a.created_at);

export function LlmopsPrompts() {
  const {baseUrl, workspace} = useOps();
  const ws = workspace?.id;
  const [selected, setSelected] = useState("");
  const prompts = useQuery({queryKey: ["llmops", "prompts", ws], enabled: Boolean(ws), queryFn: () => api.llmopsPrompts(baseUrl, ws!)});
  const promptId = selected || prompts.data?.[0]?.id || "";
  return <div className="page-stack llmops">
    <LlmopsHeader title="Prompts" intro="Immutable prompt versions. Publishing requires a passing evaluation gate; rollback is instant and audited."/>
    <Async query={prompts} what="prompts" isEmpty={(rows) => rows.length === 0} empty={<><h3>No prompts registered</h3><p>Prompt definitions are seeded by the platform.</p></>}>
      {(rows) => <>
        <div className="filters"><label>Prompt<select value={promptId} onChange={(event) => setSelected(event.target.value)}>{rows.map((row) => <option key={row.id} value={row.id}>{row.name} ({row.workflow}, default {row.default_version ?? "none"})</option>)}</select></label></div>
        {promptId && <PromptPanel key={promptId} promptId={promptId}/>}
      </>}
    </Async>
  </div>;
}

function PromptPanel({promptId}: {promptId: string}) {
  const {baseUrl, workspace} = useOps();
  const ws = workspace!.id;
  const detail = useQuery({queryKey: ["llmops", "prompt", ws, promptId], queryFn: () => api.llmopsPrompt(baseUrl, ws, promptId)});
  return <Async query={detail} what="prompt versions" isEmpty={(prompt) => prompt.versions.length === 0}>
    {(prompt) => <div className="page-stack">
      <section className="panel"><h3>{prompt.name}</h3><p className="muted">{prompt.description} Workflow {prompt.workflow}, node {prompt.node_name}.</p><VersionsTable prompt={prompt}/></section>
      <DiffPanel versions={[...prompt.versions].sort(byNewest)}/>
      <PublishPanel prompt={prompt}/>
      <RollbackPanel prompt={prompt}/>
    </div>}
  </Async>;
}

function VersionsTable({prompt}: {prompt: PromptDetail}) {
  return <div className="table-wrap"><table>
    <caption>Versions of {prompt.name} (immutable)</caption>
    <thead><tr><th scope="col">Version</th><th scope="col">Status</th><th scope="col">Default</th><th scope="col">Hash</th><th scope="col">Created</th><th scope="col">Notes</th></tr></thead>
    <tbody>{[...prompt.versions].sort(byNewest).map((version) => <tr key={version.id}>
      <th scope="row">{version.semantic_version}</th><td><span className={`status-badge ${version.status === "published" ? "obs-ok" : version.status === "retired" ? "" : "obs-warn"}`}>{version.status}</span></td>
      <td>{version.is_default ? "✓ Default" : "No"}</td><td><code>{hashPrefix(version.content_hash)}</code></td><td>{fmtDate(version.created_at)}</td><td className="wrap-cell">{version.change_notes}{version.override_reason && <span className="muted"> Published with override: {version.override_reason}</span>}</td>
    </tr>)}</tbody>
  </table></div>;
}

function DiffPanel({versions}: {versions: PromptVersion[]}) {
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  if (versions.length < 2) return <section className="panel empty"><p>At least two versions are needed to show a diff.</p></section>;
  const from = versions.find((v) => v.id === fromId) ?? versions[1]!;
  const to = versions.find((v) => v.id === toId) ?? versions[0]!;
  const parts = [["System template", from.system_template, to.system_template], ...(from.developer_template || to.developer_template ? [["Developer template", from.developer_template ?? "", to.developer_template ?? ""]] : [])] as const;
  const option = (v: PromptVersion) => <option key={v.id} value={v.id}>{v.semantic_version} ({v.status})</option>;
  return <section className="panel" aria-labelledby="diff-heading"><h3 id="diff-heading">Compare versions</h3>
    <div className="filters"><label>From<select value={from.id} onChange={(event) => setFromId(event.target.value)}>{versions.map(option)}</select></label><label>To<select value={to.id} onChange={(event) => setToId(event.target.value)}>{versions.map(option)}</select></label></div>
    {parts.map(([title, a, b]) => {
      const lines = diffLines(a, b);
      const stats = diffStats(lines);
      return <div key={title} className="text-diff" role="group" aria-label={`${title} diff`}>
        <h4>{title} <span className="muted">+{stats.added} / −{stats.removed}</span></h4>
        {stats.added + stats.removed === 0 ? <p className="muted">No differences.</p> : <div className="diff-scroll"><table className="diff-table"><caption className="sr-only">{title} unified diff from {from.semantic_version} to {to.semantic_version}</caption><tbody>
          {lines.map((line, index) => <tr key={index} className={line.kind === "add" ? "diff-add" : line.kind === "del" ? "diff-del" : undefined}><td className="diff-no">{line.oldNo ?? ""}</td><td className="diff-no">{line.newNo ?? ""}</td><td className="diff-mark" aria-label={line.kind === "add" ? "added" : line.kind === "del" ? "removed" : "unchanged"}>{line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}</td><td>{line.text}</td></tr>)}
        </tbody></table></div>}
      </div>;
    })}
  </section>;
}

function PublishPanel({prompt}: {prompt: PromptDetail}) {
  const {baseUrl, workspace, role, permissions} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const allowed = can({permissions}, "llmops.prompts.manage");
  const isOwner = role === "owner";
  const candidates = [...prompt.versions].sort(byNewest).filter((v) => !v.is_default && (v.status === "draft" || v.status === "candidate"));
  const [versionId, setVersionId] = useState("");
  const [runId, setRunId] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const runs = useQuery({queryKey: ["llmops", "eval-runs", ws], queryFn: () => api.llmopsEvalRuns(baseUrl, ws)});
  const runDetail = useQuery({queryKey: ["llmops", "eval-run", ws, runId], enabled: Boolean(runId), queryFn: () => api.llmopsEvalRun(baseUrl, ws, runId)});
  const publish = useMutation({
    mutationFn: (data: {version_id: string; eval_run_id: string; override_reason?: string}) => api.llmopsPublish(baseUrl, ws, prompt.id, data),
    onSuccess: async (result) => {
      setConfirming(false); setReason(""); setDone(`Published ${result.semantic_version} as the default.${result.overridden_gates.length ? ` Overridden gates: ${result.overridden_gates.join(", ")}.` : ""}`);
      await queryClient.invalidateQueries({queryKey: ["llmops"]});
    },
    onError: () => setConfirming(false),
  });
  if (!allowed) return <section className="panel"><h3>Publish</h3><p className="muted">Publishing prompts requires the prompt manager permission.</p></section>;
  const version = candidates.find((v) => v.id === versionId) ?? candidates[0];
  const completed = (runs.data ?? []).filter((run) => run.status === "completed");
  const run = runDetail.data;
  const {hardFailed, softFailed} = gateSummary(run?.gate_report ?? null);
  const evaluated = run ? Object.values(run.prompt_versions).some((value) => value === version?.id) : false;
  const blockers: string[] = [];
  if (!version) blockers.push("There is no unpublished version to publish.");
  if (!runId) blockers.push("Select a completed evaluation run that evaluated this version.");
  else if (runDetail.isLoading) blockers.push("Loading the evaluation run…");
  else if (runDetail.isError) blockers.push("The evaluation run could not be loaded.");
  else if (run) {
    if (!run.gate_report) blockers.push("The evaluation run has no gate report.");
    if (!evaluated) blockers.push("This evaluation run did not evaluate the selected version.");
    if (hardFailed.length) blockers.push(`Safety gates failed (${hardFailed.map((g) => humanize(g.gate)).join(", ")}). Hard gates can never be overridden.`);
    else if (softFailed.length && !isOwner) blockers.push(`Quality gates failed (${softFailed.map((g) => humanize(g.gate)).join(", ")}). Only an owner can override soft gates.`);
    else if (softFailed.length && reason.trim().length < 10) blockers.push("Enter an override reason of at least 10 characters.");
  }
  const overridable = Boolean(run) && hardFailed.length === 0 && softFailed.length > 0 && isOwner;
  const current = prompt.versions.find((v) => v.is_default);
  return <section className="panel" aria-labelledby="publish-heading"><h3 id="publish-heading">Publish a version</h3>
    {candidates.length === 0 ? <p className="muted">No draft or candidate versions are waiting to be published.</p> : <div className="form">
      <div className="form-grid">
        <label>Version to publish<select value={version?.id ?? ""} onChange={(event) => setVersionId(event.target.value)}>{candidates.map((v) => <option key={v.id} value={v.id}>{v.semantic_version} ({v.status})</option>)}</select></label>
        <label>Evaluation run (gate evidence)<select value={runId} onChange={(event) => setRunId(event.target.value)}><option value="">Select a completed run…</option>{completed.map((r) => <option key={r.id} value={r.id}>{fmtDate(r.created_at)} · {r.variant} · {r.passed_cases}/{r.total_cases} passed · {r.id.slice(0, 8)}</option>)}</select></label>
      </div>
      {runId && <Async query={runDetail} what="evaluation run">{(detail) => <div className="page-stack"><h4>Gate results</h4><GateReportView report={detail.gate_report}/><h4>Evaluation comparison</h4><EvalComparison run={detail}/></div>}</Async>}
      {overridable && <label>Override reason (owner-only, recorded in the audit log)<textarea value={reason} maxLength={500} rows={3} onChange={(event) => setReason(event.target.value)}/></label>}
      {blockers.length > 0 && <div className="notice" id="publish-blockers" role="status"><strong>Publish is disabled:</strong><ul>{blockers.map((text) => <li key={text}>{text}</li>)}</ul></div>}
      <div><button type="button" className="button primary" disabled={blockers.length > 0 || publish.isPending} aria-describedby={blockers.length ? "publish-blockers" : undefined} onClick={() => { publish.reset(); setDone(null); setConfirming(true); }}>Publish version…</button></div>
    </div>}
    <Notice error={publish.error} ok={done}/>
    {confirming && version && <ConfirmDialog title={`Publish ${version.semantic_version}?`} confirmLabel="Publish" busy={publish.isPending} onCancel={() => setConfirming(false)}
      onConfirm={() => publish.mutate({version_id: version.id, eval_run_id: runId, ...(softFailed.length ? {override_reason: reason.trim()} : {})})}>
      <p>Version <strong>{version.semantic_version}</strong> becomes the default <strong>{prompt.name}</strong> prompt for the {prompt.workflow} workflow immediately. New runs will use it{current ? <>; the current default {current.semantic_version} stops being the default</> : null}.</p>
      {softFailed.length > 0 && <p className="error">This publishes despite failed soft gates ({softFailed.map((g) => humanize(g.gate)).join(", ")}). The override and your reason are audited.</p>}
    </ConfirmDialog>}
  </section>;
}

function RollbackPanel({prompt}: {prompt: PromptDetail}) {
  const {baseUrl, workspace, permissions} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const allowed = can({permissions}, "llmops.prompts.manage");
  const targets = [...prompt.versions].sort(byNewest).filter((v) => !v.is_default && (v.status === "published" || v.status === "retired"));
  const [targetId, setTargetId] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const rollback = useMutation({
    mutationFn: (data: {target_version_id: string; reason: string}) => api.llmopsRollback(baseUrl, ws, prompt.id, data),
    onSuccess: async (result) => { setConfirming(false); setReason(""); setDone(`Rolled back: ${result.semantic_version} is now the default.`); await queryClient.invalidateQueries({queryKey: ["llmops"]}); },
    onError: () => setConfirming(false),
  });
  if (!allowed) return null;
  const target = targets.find((v) => v.id === targetId) ?? targets[0];
  const current = prompt.versions.find((v) => v.is_default);
  const valid = Boolean(target) && reason.trim().length >= 10;
  return <section className="panel" aria-labelledby="rollback-heading"><h3 id="rollback-heading">Roll back</h3>
    {targets.length === 0 ? <p className="muted">There is no earlier published version to roll back to.</p> : <div className="form">
      <label>Roll back to<select value={target?.id ?? ""} onChange={(event) => setTargetId(event.target.value)}>{targets.map((v) => <option key={v.id} value={v.id}>{v.semantic_version} ({v.status})</option>)}</select></label>
      <label>Reason (at least 10 characters)<textarea value={reason} maxLength={500} rows={2} onChange={(event) => setReason(event.target.value)}/></label>
      <div><button type="button" className="button danger" disabled={!valid || rollback.isPending} onClick={() => { rollback.reset(); setDone(null); setConfirming(true); }}>Roll back…</button></div>
    </div>}
    <Notice error={rollback.error} ok={done}/>
    {confirming && target && <ConfirmDialog title={`Roll back to ${target.semantic_version}?`} confirmLabel="Roll back" busy={rollback.isPending} onCancel={() => setConfirming(false)} onConfirm={() => rollback.mutate({target_version_id: target.id, reason: reason.trim()})}>
      <p>{target.semantic_version} becomes the default <strong>{prompt.name}</strong> prompt immediately{current ? <>, replacing {current.semantic_version}</> : null}. This takes effect for new runs without a new evaluation and is audited.</p>
    </ConfirmDialog>}
  </section>;
}
