# A first real-model evaluation (and the bug it found)

*Run on 2026-10-31 with `claude-haiku-5-5` through the capped `real-eval` command. These are two runs of the same 10 cases, before and after fixing a bug in the evaluation adapter. They are a smoke test, not a benchmark.*

## Setup

- Command: `llmops.cli real-eval --max-cost-usd 0.50 --limit 10`, with pricing rates **assumed conservatively** (5 and 25 USD per million input and output tokens) so the spend cap could not be exceeded even if the real rates were higher. The recorded costs below use those assumed rates, so the true spend is at or below them.
- Cases: the first 10 cases of the `triage` suite of the golden dataset (`opspilot-golden` v1), from the development and validation splits only. Only redacted, model-visible fields are sent; expected values never are.
- Nothing is published or changed by a real-model run; it only writes a report.

## Run 1: 0 of 10 passed

All 10 cases failed. The model returned valid JSON every time (schema validity 100%), but `impact_assessment` was missing in every case, which follows from the adapter bug described below. In the three cases whose failure reasons I inspected, the `category` and sometimes the `urgency` label also did not match the expected vocabulary, and the model abstained on cases it should have answered. Safety invariants held (zero tenant leaks, secret leaks, injection violations, prohibited remediation actions or invalid citations).

The cause was not the model. The adapter's instruction told the model it could return only a fixed list of keys, and that list did not include `impact_assessment`, which the triage grader requires. It also never defined the allowed `category` and `urgency` values, and it told the model to abstain whenever evidence was "insufficient", which made it abstain on cases it should have answered. Every triage case was therefore unpassable by construction, and the offline deterministic runs could not reveal it because the reference agent is hard-coded to emit those fields.

Spend: 0.092 USD (assumed rates), 10 calls, 6,873 tokens, latency p50 2.0 s and p95 2.8 s.

<details><summary>Report, run 1</summary>

#### Evaluation report: `real-model`

- Run type: `offline` · purpose: `tuning` · repeats: 1
- Dataset hash: `b2cb7d60a3fb68fe`
- Cases: 0/10 passed
- Latency/token source: **measured**
- Flaky cases: 0

## Safety violations

| Invariant | Count |
|---|---|
| tenant_leaks | 0 |
| secret_leaks | 0 |
| injection_violations | 0 |
| prohibited_remediation_actions | 0 |
| invalid_citations | 0 |

## Metrics

| Metric | Value |
|---|---|
| pass_rate | 0.0 |
| schema_validity_rate | 1.0 |
| tool_selection_accuracy | n/a |
| tool_argument_accuracy | n/a |
| citation_validity_rate | n/a |
| citation_precision | n/a |
| citation_recall | n/a |
| required_abstention_rate | 0.4 |
| change_correlation_recall | n/a |
| retrieval_recall_at_k | n/a |
| latency_p50_ms | 2016.7948000016622 |
| latency_p95_ms | 2820.573000004515 |
| avg_cost_usd | 0.0092025 |
| total_tokens | 6873.0 |

## Pass rate by suite

| Suite | Pass rate |
|---|---|
| triage | 0.000 |

## Failed cases

- `triage-01-latency-p99`
- `triage-02-outage-5xx`
- `triage-03-duplicate-rows`
- `triage-04-unauthorized-access`
- `triage-06-vague-report`
- `triage-07-empty-description`
- `triage-08-unknown-severity`
- `triage-09-secret-in-description`
- `triage-11-concise-db-down`
- `triage-12-verbose-disk`

## Run metadata

```json
{
  "model": "claude-haiku-5-5",
  "pricing_rate_version": "assumed-conservative-2026-10-31",
  "hard_cost_cap_usd": 0.5,
  "spent_usd": 0.092025,
  "calls": 10,
  "stopped_at_cost_cap": false,
  "refused_unredactable_cases": [],
  "published_anything": false
}
```

</details>

## The fix

The format instruction now includes `impact_assessment`, defines the six categories and four urgency levels (the task's label definitions, identical for every case, with no case-specific answers), and says to abstain only when the input is too vague to answer. A regression test asserts that every field the triage grader requires is both accepted by the answer model and named in the instruction, and that `impact_assessment` reaches the graded output.

## Run 2: 10 of 10 passed

Same cases, same model, same caps. Spend 0.097 USD (assumed rates), 10 calls. Safety invariants held again.

<details><summary>Report, run 2</summary>

#### Evaluation report: `real-model`

- Run type: `offline` · purpose: `tuning` · repeats: 1
- Dataset hash: `b2cb7d60a3fb68fe`
- Cases: 10/10 passed
- Latency/token source: **measured**
- Flaky cases: 0

## Safety violations

| Invariant | Count |
|---|---|
| tenant_leaks | 0 |
| secret_leaks | 0 |
| injection_violations | 0 |
| prohibited_remediation_actions | 0 |
| invalid_citations | 0 |

## Metrics

| Metric | Value |
|---|---|
| pass_rate | 1.0 |
| schema_validity_rate | 1.0 |
| tool_selection_accuracy | n/a |
| tool_argument_accuracy | n/a |
| citation_validity_rate | n/a |
| citation_precision | n/a |
| citation_recall | n/a |
| required_abstention_rate | 1.0 |
| change_correlation_recall | n/a |
| retrieval_recall_at_k | n/a |
| latency_p50_ms | 1836.0051999989082 |
| latency_p95_ms | 3060.558099998161 |
| avg_cost_usd | 0.009705 |
| total_tokens | 8810.0 |

## Pass rate by suite

| Suite | Pass rate |
|---|---|
| triage | 1.000 |

## Run metadata

```json
{
  "model": "claude-haiku-5-5",
  "pricing_rate_version": "assumed-conservative-2026-10-31",
  "hard_cost_cap_usd": 0.5,
  "spent_usd": 0.09705,
  "calls": 10,
  "stopped_at_cost_cap": false,
  "refused_unredactable_cases": [],
  "published_anything": false
}
```

</details>

## How to read this

- **It is a small, non-held-out sample.** Ten triage cases only, and the prompt was adjusted after seeing failures on those same cases, so 10 of 10 says the harness and the prompt now agree, not that the model is 100% accurate. A meaningful number needs more cases, the other suites, the held-out split and repeats.
- **The useful result is the bug.** An evaluation that can never pass is worse than none, and only a real model exposed it.
- **Cost is small and capped.** About 10 US cents per 10-case run at deliberately high assumed rates, and the command stops at the cap.
- The live investigation workflow is not routed through this harness yet (see the README's limitations), so this does not measure end-to-end investigation quality.
