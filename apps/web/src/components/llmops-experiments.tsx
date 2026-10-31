"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, EXPERIMENT_METRICS, type EvalVariant, type Experiment, type ExperimentCreate } from "@/lib/api";
import { can } from "@/lib/permissions";
import { Async, fmtDate, fmtPct, humanize, LlmopsHeader, Notice } from "./llmops-common";
import { useOps } from "./providers";

const VARIANTS: EvalVariant[] = ["baseline", "candidate", "regressing"];
const label = (spec: Record<string, unknown>) => typeof spec.label === "string" ? spec.label : "unnamed";
const statusTone = (status: string) => status === "running" ? "obs-ok" : status === "paused" || status === "draft" ? "obs-warn" : "";

export function LlmopsExperiments() {
  const {baseUrl, workspace, permissions} = useOps();
  const ws = workspace?.id;
  const [selected, setSelected] = useState<string | null>(null);
  const experiments = useQuery({queryKey: ["llmops", "experiments", ws], enabled: Boolean(ws), queryFn: () => api.llmopsExperiments(baseUrl, ws!)});
  return <div className="page-stack llmops">
    <LlmopsHeader title="Experiments" intro="Controlled A/B comparisons of a candidate against the control. Results are advisory: promotion is always a manual, audited decision."/>
    {can({permissions}, "llmops.experiments.manage") && <CreateForm onCreated={setSelected}/>}
    <Async query={experiments} what="experiments" isEmpty={(rows) => rows.length === 0} empty={<><h3>No experiments</h3><p>Create an experiment to compare a candidate with the control on live traffic.</p></>}>
      {(rows) => <div className="table-wrap panel"><table>
        <caption>Experiments</caption>
        <thead><tr><th scope="col">Name</th><th scope="col">Status</th><th scope="col">Workflow</th><th scope="col">Control vs candidate</th><th scope="col">Allocation</th><th scope="col">Created</th><th scope="col">Details</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.id}>
          <th scope="row">{row.name}</th><td><span className={`status-badge ${statusTone(row.status)}`}>{row.status}</span></td><td>{row.workflow}</td>
          <td>{label(row.control_variant)} vs {label(row.candidate_variant)}</td><td>{row.allocation_percentage}% to candidate</td><td>{fmtDate(row.created_at)}</td>
          <td><button type="button" className="button" aria-pressed={selected === row.id} onClick={() => setSelected(row.id)}>View {row.name}</button></td>
        </tr>)}</tbody>
      </table></div>}
    </Async>
    {selected && <ExperimentDetailView key={selected} id={selected}/>}
  </div>;
}

function ExperimentDetailView({id}: {id: string}) {
  const {baseUrl, workspace, permissions} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const manage = can({permissions}, "llmops.experiments.manage");
  const detail = useQuery({queryKey: ["llmops", "experiment", ws, id], queryFn: () => api.llmopsExperiment(baseUrl, ws, id)});
  const refresh = () => queryClient.invalidateQueries({queryKey: ["llmops"]});
  const action = useMutation({mutationFn: (name: "start" | "pause") => api.llmopsExperimentAction(baseUrl, ws, id, name), onSuccess: refresh});
  const stop = useMutation({mutationFn: (data: {reason: string; safety: boolean}) => api.llmopsStopExperiment(baseUrl, ws, id, data), onSuccess: refresh});
  const [reason, setReason] = useState("");
  const [safety, setSafety] = useState(false);
  return <Async query={detail} what="experiment">{(exp) => {
    const a = exp.analysis;
    const open = exp.status === "draft" || exp.status === "running" || exp.status === "paused";
    return <section className="panel page-stack" aria-labelledby="exp-heading">
      <h3 id="exp-heading">{exp.name}</h3>
      <p><span className={`status-badge ${statusTone(exp.status)}`}>{exp.status}</span> {exp.workflow} · control <strong>{label(exp.control_variant)}</strong> vs candidate <strong>{label(exp.candidate_variant)}</strong> · {exp.allocation_percentage}% allocated to candidate · unit {exp.unit}</p>
      {exp.stop_reason && <p className="notice">Stopped: {exp.stop_reason}</p>}
      <p className="muted">Primary metric {humanize(exp.primary_metric)}; guardrails {exp.guardrail_metrics.map(humanize).join(", ")}. Assignments: {Object.entries(exp.assignment_counts).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet"}.</p>
      {a?.safety_stop && <div className="failure-box" role="alert"><strong>✕ Safety stop.</strong> A safety regression was detected in the candidate arm. Stop this experiment now.</div>}
      {a ? <>
        <p><strong>Sample size:</strong> control {a.control_n}, candidate {a.candidate_n}, minimum {a.minimum_sample_size} per arm. {a.sample_size_reached ? "✓ Minimum reached." : "Minimum not yet reached."}</p>
        {a.warnings.length > 0 && <div className="notice" role="status"><strong>Warnings</strong><ul>{a.warnings.map((w) => <li key={w}>{w}</li>)}</ul></div>}
        {a.metrics.length === 0 ? <p className="muted">No metric comparisons yet.</p> : <div className="table-wrap"><table>
          <caption>Candidate minus control, with 95% confidence intervals</caption>
          <thead><tr><th scope="col">Metric</th><th scope="col">Role</th><th scope="col">Control</th><th scope="col">Candidate</th><th scope="col">Difference</th><th scope="col">Confidence interval</th><th scope="col">p-value</th><th scope="col">Verdict</th></tr></thead>
          <tbody>{a.metrics.map((m) => <tr key={m.metric}>
            <th scope="row">{humanize(m.metric)}</th><td>{m.guardrail ? "Guardrail" : "Primary"}</td><td>{fmtPct(m.control_rate)}</td><td>{fmtPct(m.candidate_rate)}</td>
            <td>{(m.difference * 100).toFixed(2)} pp</td><td>[{(m.ci_low * 100).toFixed(2)}, {(m.ci_high * 100).toFixed(2)}] pp</td><td>{m.p_value === null ? "n/a" : m.p_value.toFixed(4)}</td>
            <td>{m.regressed ? <span className="status-badge obs-bad">✕ Regressed</span> : m.significant ? <span className="status-badge obs-ok">✓ Significant</span> : "Not significant"}<span className="tag-line">{m.note}</span></td>
          </tr>)}</tbody>
        </table></div>}
        <section aria-label="Statistical assumptions"><h4>Statistical assumptions</h4><ul>{a.assumptions.map((x) => <li key={x}>{x}</li>)}</ul></section>
        <p className="advisory">Promotion is manual. {a.promotion_note}</p>
      </> : <p className="muted">No analysis is available yet: the experiment has not collected data.</p>}
      {manage && open && <div className="review-form">
        <div className="review-actions">
          {(exp.status === "draft" || exp.status === "paused") && <button type="button" className="button primary" disabled={action.isPending} onClick={() => action.mutate("start")}>{exp.status === "draft" ? "Start" : "Resume"} experiment</button>}
          {exp.status === "running" && <button type="button" className="button" disabled={action.isPending} onClick={() => action.mutate("pause")}>Pause experiment</button>}
        </div>
        <label>Stop reason (3 to 200 characters)<input value={reason} maxLength={200} onChange={(event) => setReason(event.target.value)}/></label>
        <label className="check-row"><input type="checkbox" checked={safety} onChange={(event) => setSafety(event.target.checked)}/> Safety stop (the candidate caused a safety problem)</label>
        <div><button type="button" className="button danger" disabled={stop.isPending || reason.trim().length < 3} onClick={() => stop.mutate({reason: reason.trim(), safety})}>Stop experiment</button></div>
      </div>}
      <Notice error={action.error ?? stop.error}/>
    </section>;
  }}</Async>;
}

function CreateForm({onCreated}: {onCreated: (id: string) => void}) {
  const {baseUrl, workspace} = useOps();
  const ws = workspace!.id;
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [workflow, setWorkflow] = useState("investigation");
  const [control, setControl] = useState<{label: string; eval_variant: EvalVariant}>({label: "control", eval_variant: "baseline"});
  const [candidate, setCandidate] = useState<{label: string; eval_variant: EvalVariant}>({label: "candidate", eval_variant: "candidate"});
  const [allocation, setAllocation] = useState(10);
  const [unit, setUnit] = useState<ExperimentCreate["unit"]>("investigation");
  const [risks, setRisks] = useState<Array<"low" | "medium">>(["low", "medium"]);
  const [primary, setPrimary] = useState<string>("pass_rate");
  const [guardrails, setGuardrails] = useState<string[]>(["safety_rate"]);
  const [minimum, setMinimum] = useState(100);
  const create = useMutation({
    mutationFn: (data: ExperimentCreate) => api.llmopsCreateExperiment(baseUrl, ws, data),
    onSuccess: async (exp: Experiment) => { setName(""); await queryClient.invalidateQueries({queryKey: ["llmops", "experiments"]}); onCreated(exp.id); },
  });
  const toggle = <T extends string>(list: T[], value: T, on: boolean) => on ? [...list, value] : list.filter((v) => v !== value);
  const valid = name.trim().length >= 3 && guardrails.length > 0 && allocation > 0 && allocation <= 50 && minimum >= 30;
  return <section className="panel" aria-labelledby="create-exp-heading"><h3 id="create-exp-heading">Create an experiment</h3>
    <form className="form" onSubmit={(event) => { event.preventDefault(); create.mutate({name: name.trim(), workflow, control_variant: control, candidate_variant: candidate, allocation_percentage: allocation, unit, allowed_risks: risks, primary_metric: primary, guardrail_metrics: guardrails, minimum_sample_size: minimum}); }}>
      <div className="form-grid">
        <label>Name<input value={name} minLength={3} maxLength={120} onChange={(event) => setName(event.target.value)}/></label>
        <label>Workflow<input value={workflow} maxLength={60} onChange={(event) => setWorkflow(event.target.value)}/></label>
        <label>Control label<input value={control.label} maxLength={60} onChange={(event) => setControl({...control, label: event.target.value})}/></label>
        <label>Control variant<select value={control.eval_variant} onChange={(event) => setControl({...control, eval_variant: event.target.value as EvalVariant})}>{VARIANTS.map((v) => <option key={v}>{v}</option>)}</select></label>
        <label>Candidate label<input value={candidate.label} maxLength={60} onChange={(event) => setCandidate({...candidate, label: event.target.value})}/></label>
        <label>Candidate variant<select value={candidate.eval_variant} onChange={(event) => setCandidate({...candidate, eval_variant: event.target.value as EvalVariant})}>{VARIANTS.map((v) => <option key={v}>{v}</option>)}</select></label>
        <label>Allocation to candidate (% , above 0 up to 50)<input type="number" min={1} max={50} step="any" value={allocation} onChange={(event) => setAllocation(Number(event.target.value))}/></label>
        <label>Assignment unit<select value={unit} onChange={(event) => setUnit(event.target.value as ExperimentCreate["unit"])}><option>investigation</option><option>user</option><option>workspace</option></select></label>
        <label>Primary metric<select value={primary} onChange={(event) => setPrimary(event.target.value)}>{EXPERIMENT_METRICS.map((m) => <option key={m} value={m}>{m}</option>)}</select></label>
        <label>Minimum sample size per arm (30 or more)<input type="number" min={30} value={minimum} onChange={(event) => setMinimum(Number(event.target.value))}/></label>
      </div>
      <fieldset><legend>Guardrail metrics (at least one)</legend>{EXPERIMENT_METRICS.map((m) => <label key={m} className="check-row"><input type="checkbox" checked={guardrails.includes(m)} onChange={(event) => setGuardrails(toggle(guardrails, m, event.target.checked))}/> {m}</label>)}</fieldset>
      <fieldset><legend>Eligible risk levels</legend>{(["low", "medium"] as const).map((r) => <label key={r} className="check-row"><input type="checkbox" checked={risks.includes(r)} onChange={(event) => setRisks(toggle(risks, r, event.target.checked))}/> {r}</label>)}</fieldset>
      <div><button type="submit" className="button primary" disabled={!valid || create.isPending}>{create.isPending ? "Creating…" : "Create experiment"}</button></div>
      <Notice error={create.error}/>
    </form>
  </section>;
}
