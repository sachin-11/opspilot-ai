"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api, type EvalResult, type EvalRunDetail } from "@/lib/api";
import { can } from "@/lib/permissions";
import { Async, BarList, fmtDate, fmtInt, fmtMs, fmtPct, fmtUsd, humanize, LlmopsHeader, Metric, Notice } from "./llmops-common";
import { EvalComparison, GateReportView } from "./llmops-eval-parts";
import { statusTone } from "./llmops-evaluations";
import { useOps } from "./providers";

export const RUBRIC_VERSION = "judge-rubric-v1";
export const RUBRIC_CRITERIA = ["factual_consistency", "root_cause_quality", "relevance", "completeness", "actionability", "appropriate_uncertainty"] as const;

function download(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = document.createElement("a");
  link.href = url; link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
  URL.revokeObjectURL(url);
}

export function LlmopsEvalDetail({evalRunId}: {evalRunId: string}) {
  const {baseUrl, workspace} = useOps();
  const ws = workspace?.id;
  const run = useQuery({
    queryKey: ["llmops", "eval-run", ws, evalRunId], enabled: Boolean(ws), queryFn: () => api.llmopsEvalRun(baseUrl, ws!, evalRunId),
    refetchInterval: (query) => query.state.data && ["queued", "running"].includes(query.state.data.status) ? 4000 : false,
  });
  return <div className="page-stack llmops">
    <LlmopsHeader title={`Evaluation ${evalRunId.slice(0, 8)}`} intro="Quality, safety, and cost report for one evaluation run."/>
    <p><Link href="/llmops/evaluations">← All evaluation runs</Link></p>
    <Async query={run} what="evaluation run">{(data) => <RunReport run={data}/>}</Async>
  </div>;
}

function RunReport({run}: {run: EvalRunDetail}) {
  const synthetic = run.metrics_source === "synthetic";
  const done = run.status === "completed";
  const rate = run.total_cases > 0 ? run.passed_cases / run.total_cases : null;
  const suites = Object.entries(run.by_suite).map(([label, value]) => ({label, value}));
  const tags = Object.entries(run.by_tag).map(([label, value]) => ({label, value}));
  const graders = Object.entries(run.grader_breakdown).map(([label, value]) => ({label: humanize(label), value}));
  return <>
    <section className="panel" aria-labelledby="summary-heading"><h3 id="summary-heading">Summary</h3>
      <p><span className={`status-badge ${statusTone(run.status)}`}>{run.status}</span> variant {run.variant} · {run.run_type} / {run.purpose} · dataset v{run.dataset_version} · {run.repeat_count} repeat(s) · created {fmtDate(run.created_at)}</p>
      {run.error && <p className="error" role="alert">Run error: {run.error}</p>}
      {!done && <p className="notice" role="status" aria-live="polite">This run is {run.status}; results appear when it completes.</p>}
      <div className="metric-grid llmops-grid">
        <Metric label="Passed" value={`${run.passed_cases}/${run.total_cases}`} note={rate === null ? "No cases scored" : fmtPct(rate)}/>
        <Metric label="Failed" value={run.failed_cases}/>
        <Metric label="Tokens" value={fmtInt(run.total_tokens)} synthetic={synthetic}/>
        <Metric label="Latency P50 / P95" value={`${fmtMs(run.metrics.latency_p50_ms ?? null)} / ${fmtMs(run.metrics.latency_p95_ms ?? null)}`} synthetic={synthetic}/>
        <Metric label="Avg cost per case" value={fmtUsd(run.metrics.avg_cost_usd ?? null)} synthetic={synthetic && run.metrics.avg_cost_usd != null}/>
      </div>
      <ExportButtons run={run}/>
    </section>
    {done && <>
      <section className="panel" aria-labelledby="gates-heading"><h3 id="gates-heading">Gate results</h3><GateReportView report={run.gate_report}/></section>
      <section className="panel" aria-labelledby="cmp-heading"><h3 id="cmp-heading">Metrics and comparison</h3><EvalComparison run={run}/></section>
      <section className="panel"><h3>Metrics by suite</h3>{suites.length ? <BarList caption="Pass rate by suite" items={suites} format={fmtPct}/> : <p className="muted">No suite data.</p>}</section>
      <section className="panel"><h3>Metrics by tag</h3>{tags.length ? <BarList caption="Pass rate by tag" items={tags} format={fmtPct}/> : <p className="muted">No tag data.</p>}</section>
      <section className="panel"><h3>Grader breakdown</h3>{graders.length ? <BarList caption="Mean deterministic grader score" items={graders} format={fmtPct}/> : <p className="muted">No grader scores recorded.</p>}</section>
      <section className="panel" aria-labelledby="failed-heading"><h3 id="failed-heading">Failed cases</h3><FailedCases results={run.failed_results}/></section>
      <section className="panel" aria-labelledby="flaky-heading"><h3 id="flaky-heading">Repeated-run consistency</h3>
        {run.repeat_count < 2 ? <p className="muted">This run used a single repeat, so consistency was not measured.</p> : run.flaky_cases.length === 0 ? <p>All {run.repeat_count} repeats agreed: no flaky cases.</p> : <><p className="error">{run.flaky_cases.length} flaky case(s) gave different outcomes across repeats:</p><ul>{run.flaky_cases.map((key) => <li key={key}><code>{key}</code></li>)}</ul></>}
      </section>
      <LabelQueue run={run}/>
    </>}
  </>;
}

function ExportButtons({run}: {run: EvalRunDetail}) {
  const name = `eval-run-${run.id.slice(0, 8)}`;
  return <div className="review-actions">
    <button type="button" className="button" onClick={() => download(`${name}.json`, JSON.stringify(run, null, 2), "application/json")}>Download run JSON</button>
    <button type="button" className="button" disabled={!run.markdown_report} onClick={() => download(`${name}.md`, run.markdown_report ?? "", "text/markdown")}>Download markdown report</button>
  </div>;
}

function FailedCases({results}: {results: EvalResult[]}) {
  if (results.length === 0) return <p className="muted">No failed cases.</p>;
  return <div className="table-wrap"><table>
    <caption>Failed cases and failure reasons</caption>
    <thead><tr><th scope="col">Case</th><th scope="col">Suite</th><th scope="col">Failure reasons</th></tr></thead>
    <tbody>{results.map((result) => <tr key={result.id}><th scope="row"><code>{result.case_key}</code></th><td>{result.suite}</td><td className="wrap-cell">{result.safety_failed && <span className="status-badge obs-bad">✕ safety failure </span>}{result.failure_reasons.length ? result.failure_reasons.join("; ") : "No reason recorded"}</td></tr>)}</tbody>
  </table></div>;
}

function LabelQueue({run}: {run: EvalRunDetail}) {
  const {permissions} = useOps();
  const canLabel = can({permissions}, "llmops.evals.label");
  const [saved, setSaved] = useState<string | null>(null);
  return <section className="panel" aria-labelledby="label-heading"><h3 id="label-heading">Human label queue</h3>
    <p className="muted">Review failed or safety-flagged cases against rubric {RUBRIC_VERSION}. Only redacted fields are shown. Human labels calibrate the automatic judge; they do not change this run&apos;s gates.</p>
    {run.label_queue.length === 0 ? <p className="muted">Nothing is waiting for a human label.</p>
      : !canLabel ? <p className="notice">{run.label_queue.length} case(s) await review. Labelling requires the reviewer permission.</p>
      : <ul className="plain-list">{run.label_queue.map((result) => <li key={result.id}><LabelForm runId={run.id} result={result} onSaved={(key) => setSaved(`Label saved for ${key}.`)}/></li>)}</ul>}
    {saved && <p className="notice" role="status">{saved}</p>}
  </section>;
}

function LabelForm({runId, result, onSaved}: {runId: string; result: EvalResult; onSaved: (caseKey: string) => void}) {
  const {baseUrl, workspace} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const [scores, setScores] = useState<Record<string, string>>({});
  const [verdict, setVerdict] = useState<"" | "pass" | "fail">("");
  const [notes, setNotes] = useState("");
  const submit = useMutation({
    mutationFn: () => api.llmopsHumanLabel(baseUrl, ws, result.id, {rubric_version: RUBRIC_VERSION, criterion_scores: Object.fromEntries(RUBRIC_CRITERIA.map((c) => [c, Number(scores[c])])), passed: verdict === "pass", notes: notes.trim() || null}),
    onSuccess: async () => { onSaved(result.case_key); await queryClient.invalidateQueries({queryKey: ["llmops", "eval-run", ws, runId]}); },
  });
  const complete = RUBRIC_CRITERIA.every((c) => scores[c]) && verdict !== "";
  return <form className="form label-form" aria-label={`Label ${result.case_key}`} onSubmit={(event) => { event.preventDefault(); submit.mutate(); }}>
    <h4><code>{result.case_key}</code> <span className="muted">suite {result.suite}</span></h4>
    {result.failure_reasons.length > 0 && <p className="muted">Automatic failure reasons: {result.failure_reasons.join("; ")}</p>}
    <div className="form-grid">{RUBRIC_CRITERIA.map((criterion) => <label key={criterion}>{humanize(criterion)} (1 to 5)
      <select value={scores[criterion] ?? ""} onChange={(event) => setScores({...scores, [criterion]: event.target.value})}><option value="">Select…</option>{[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}</select></label>)}</div>
    <fieldset><legend>Overall verdict</legend>
      <label className="check-row"><input type="radio" name={`verdict-${result.id}`} checked={verdict === "pass"} onChange={() => setVerdict("pass")}/> Pass</label>
      <label className="check-row"><input type="radio" name={`verdict-${result.id}`} checked={verdict === "fail"} onChange={() => setVerdict("fail")}/> Fail</label></fieldset>
    <label>Notes (optional, max 500 characters; do not paste secrets)<textarea rows={2} maxLength={500} value={notes} onChange={(event) => setNotes(event.target.value)}/></label>
    <div><button type="submit" className="button primary" disabled={!complete || submit.isPending}>{submit.isPending ? "Saving…" : "Submit label"}</button></div>
    <Notice error={submit.error}/>
  </form>;
}
