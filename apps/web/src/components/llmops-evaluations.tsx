"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type EvalRunCreate, type EvalRunSummary, type EvalVariant } from "@/lib/api";
import { can } from "@/lib/permissions";
import { Async, fmtDate, fmtUsd, LlmopsHeader, Notice, SyntheticTag } from "./llmops-common";
import { useOps } from "./providers";

const VARIANTS: EvalVariant[] = ["baseline", "candidate", "regressing"];
const RUN_TYPES = ["offline", "ci", "candidate_comparison", "production_sample", "shadow"] as const;
const PURPOSES = ["tuning", "development", "gate"] as const;
const ACTIVE = new Set(["queued", "running"]);

export function statusTone(status: string) {
  return status === "completed" ? "obs-ok" : status === "failed" ? "obs-bad" : status === "cancelled" ? "" : "obs-warn";
}

export function LlmopsEvaluations() {
  const {baseUrl, workspace, permissions} = useOps();
  const ws = workspace?.id;
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["llmops", "eval-runs", ws], enabled: Boolean(ws), queryFn: () => api.llmopsEvalRuns(baseUrl, ws!),
    refetchInterval: (query) => query.state.data?.some((run) => ACTIVE.has(run.status)) ? 5000 : false,
  });
  const cancel = useMutation({mutationFn: (runId: string) => api.llmopsCancelEvalRun(baseUrl, ws!, runId), onSettled: () => queryClient.invalidateQueries({queryKey: ["llmops", "eval-runs"]})});
  const canRun = can({permissions}, "llmops.evals.run");
  return <div className="page-stack llmops">
    <LlmopsHeader title="Evaluations" intro="Run the golden dataset against a prompt/model variant and review quality gates."/>
    {canRun ? <StartRunForm/> : <section className="panel"><p className="muted">Starting evaluation runs requires the evaluation runner permission.</p></section>}
    <Notice error={cancel.error}/>
    <Async query={runs} what="evaluation runs" isEmpty={(rows) => rows.length === 0} empty={<><h3>No evaluation runs</h3><p>Start a run above to evaluate a variant against the golden dataset.</p></>}>
      {(rows) => <div className="table-wrap panel"><table>
        <caption>Evaluation runs (newest first)</caption>
        <thead><tr><th scope="col">Run</th><th scope="col">Status</th><th scope="col">Variant</th><th scope="col">Type / purpose</th><th scope="col">Passed</th><th scope="col">Cost</th><th scope="col">Created</th>{canRun && <th scope="col">Actions</th>}</tr></thead>
        <tbody>{rows.map((run) => <RunRow key={run.id} run={run} canCancel={canRun} onCancel={() => cancel.mutate(run.id)}/>)}</tbody>
      </table></div>}
    </Async>
  </div>;
}

function RunRow({run, canCancel, onCancel}: {run: EvalRunSummary; canCancel: boolean; onCancel: () => void}) {
  return <tr>
    <th scope="row"><Link href={`/llmops/evaluations/${run.id}`}>{run.id.slice(0, 8)}</Link></th>
    <td><span className={`status-badge ${statusTone(run.status)}`}>{run.status}</span>{run.error && <span className="error"> {run.error}</span>}</td>
    <td>{run.variant}</td><td>{run.run_type} / {run.purpose}</td>
    <td>{run.passed_cases}/{run.total_cases}{run.failed_cases > 0 && <span className="error"> ({run.failed_cases} failed)</span>}</td>
    <td>{fmtUsd(run.estimated_cost_usd)}{run.metrics_source === "synthetic" && <> <SyntheticTag/></>}</td>
    <td>{fmtDate(run.created_at)}</td>
    {canCancel && <td>{ACTIVE.has(run.status) ? <button type="button" className="button" onClick={onCancel}>Cancel run</button> : "—"}</td>}
  </tr>;
}

function StartRunForm() {
  const {baseUrl, workspace} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const datasets = useQuery({queryKey: ["llmops", "datasets", ws], queryFn: () => api.llmopsDatasets(baseUrl, ws)});
  const [datasetId, setDatasetId] = useState("");
  const [variant, setVariant] = useState<EvalVariant>("candidate");
  const [control, setControl] = useState<"" | EvalVariant>("baseline");
  const [runType, setRunType] = useState<EvalRunCreate["run_type"]>("offline");
  const [purpose, setPurpose] = useState<EvalRunCreate["purpose"]>("tuning");
  const [repeats, setRepeats] = useState("1");
  const [started, setStarted] = useState<string | null>(null);
  const locked = runType === "ci" || runType === "candidate_comparison";
  const effectivePurpose = locked ? "gate" : purpose;
  const start = useMutation({
    mutationFn: (data: EvalRunCreate) => api.llmopsStartEvalRun(baseUrl, ws, data),
    onSuccess: async (run) => { setStarted(`Evaluation run ${run.id.slice(0, 8)} is ${run.status}.`); await queryClient.invalidateQueries({queryKey: ["llmops", "eval-runs"]}); },
  });
  const dataset = datasets.data?.find((d) => d.id === datasetId) ?? datasets.data?.[0];
  return <section className="panel" aria-labelledby="start-heading"><h3 id="start-heading">Start an evaluation run</h3>
    <Async query={datasets} what="datasets" isEmpty={(rows) => rows.length === 0} empty={<p>No published datasets are available.</p>}>
      {(rows) => <form className="form" onSubmit={(event) => { event.preventDefault(); setStarted(null); if (!dataset) return; start.mutate({dataset_id: dataset.id, variant, control_variant: control || null, run_type: runType, purpose: effectivePurpose, repeat_count: Math.min(5, Math.max(1, Math.round(Number(repeats)) || 1))}); }}>
        <div className="form-grid">
          <label>Dataset<select value={dataset?.id ?? ""} onChange={(event) => setDatasetId(event.target.value)}>{rows.map((d) => <option key={d.id} value={d.id}>{d.name} v{d.version} ({d.case_count} cases)</option>)}</select></label>
          <label>Variant<select value={variant} onChange={(event) => setVariant(event.target.value as EvalVariant)}>{VARIANTS.map((v) => <option key={v}>{v}</option>)}</select></label>
          <label>Control variant (for comparison)<select value={control} onChange={(event) => setControl(event.target.value as "" | EvalVariant)}><option value="">None</option>{VARIANTS.map((v) => <option key={v}>{v}</option>)}</select></label>
          <label>Run type<select value={runType} onChange={(event) => setRunType(event.target.value as EvalRunCreate["run_type"])}>{RUN_TYPES.map((v) => <option key={v}>{v}</option>)}</select></label>
          <label>Purpose<select value={effectivePurpose} disabled={locked} onChange={(event) => setPurpose(event.target.value as EvalRunCreate["purpose"])}>{PURPOSES.map((v) => <option key={v}>{v}</option>)}</select></label>
          <label>Repeats (1 to 5)<input type="number" min={1} max={5} value={repeats} onChange={(event) => setRepeats(event.target.value)}/></label>
        </div>
        <p className="muted">Purpose “gate” is the only valid purpose for ci and candidate_comparison runs; it is selected automatically. Use repeats of 2 or more to detect flaky cases.</p>
        <div><button type="submit" className="button primary" disabled={start.isPending || !dataset}>{start.isPending ? "Starting…" : "Start run"}</button></div>
        <Notice error={start.error} ok={started}/>
      </form>}
    </Async>
  </section>;
}
