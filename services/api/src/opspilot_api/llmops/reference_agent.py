"""Deterministic, rule-based reference system under test for the required CI tier.

It consumes only ``case.input_data`` (never expected outputs) and applies explicit policies, so
passing the golden dataset is a real property of the policies, not an echo of the answers. Variants
exist so regressions can be demonstrated: a variant that weakens a policy must fail the gates.
No model, network, or clock is used; results are fully reproducible.
"""

import math
import re
from dataclasses import dataclass
from typing import Any

from opspilot_api.investigations.safety import redact_text
from opspilot_api.llmops.eval_types import CaseOutput, EvalCaseSpec, ToolCall

_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "was",
        "were",
        "are",
        "has",
        "have",
        "had",
        "not",
        "but",
        "its",
        "into",
        "out",
        "after",
        "before",
        "during",
        "when",
        "then",
        "than",
        "over",
        "under",
        "all",
        "any",
        "can",
        "will",
        "would",
        "should",
        "could",
        "been",
        "being",
        "their",
        "there",
        "which",
        "while",
        "about",
        "also",
        "very",
        "more",
        "most",
        "some",
        "such",
        "only",
        "just",
    ]
)
_INJECTION = re.compile(
    r"(?i)(ignore (?:all )?(?:previous|prior) instructions|disregard (?:the )?(?:above|system)|"
    r"you are now|system prompt|call the tool|run the tool|execute the tool|print .*canary)"
)
_EXTRA_SECRET = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:secret|token)\s*[:=]\s*[^\s,;]{6,}"),
]
_CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "security": ("unauthorized", "breach", "leaked", "suspicious", "credential", "intrusion"),
    "outage": ("down", "5xx", "unavailable", "crash", "crashing", "outage", "unreachable"),
    "data_issue": ("corrupt", "missing", "duplicate", "stale", "inconsistent", "rows"),
    "capacity": ("cpu", "memory", "disk", "saturation", "oom", "exhausted", "throttled"),
    "service_degradation": ("latency", "slow", "timeout", "timeouts", "p99", "degraded"),
}
_URGENCY = {"sev1": "critical", "sev2": "high", "sev3": "medium", "sev4": "low"}
_CONTRADICTION = ("ruled out", "not the cause", "unrelated", "unaffected", "no evidence of")
_PROVIDER_ERRORS = {
    "retryable": ("provider_temporarily_unavailable", "failed"),
    "auth": ("provider_configuration_error", "failed"),
    "invalid_output": ("invalid_structured_output", "failed"),
}


@dataclass(frozen=True)
class Variant:
    """Policy switches. ``baseline`` is the shipped behavior; the rest are test doubles."""

    name: str
    follow_injection: bool = False
    include_foreign: bool = False
    redact_secrets: bool = True
    always_answer: bool = False
    invent_citation: bool = False
    auto_approve: bool = False
    rank_mode: str = "overlap"  # "overlap" | "bm25"


BASELINE = Variant("baseline")
CANDIDATE = Variant("candidate", rank_mode="bm25")
REGRESSING = Variant(
    "regressing",
    follow_injection=True,
    include_foreign=True,
    redact_secrets=False,
    always_answer=True,
    invent_citation=True,
    auto_approve=True,
)
VARIANTS: dict[str, Variant] = {v.name: v for v in (BASELINE, CANDIDATE, REGRESSING)}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def tokens(text: str, stem: bool = False) -> list[str]:
    words = [t for t in re.findall(r"[a-z0-9_]{3,}", text.lower()) if t not in _STOP]
    return [_stem(w) for w in words] if stem else words


def sanitize(text: str, variant: Variant) -> str:
    """Drop injected sentences and redact secrets from untrusted evidence before quoting."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if not variant.follow_injection:
        # Quarantine: everything from the first injected sentence onward is untrusted.
        kept: list[str] = []
        for sentence in sentences:
            if _INJECTION.search(sentence):
                break
            kept.append(sentence)
        sentences = kept
    cleaned = " ".join(sentences)
    if variant.redact_secrets:
        cleaned = redact_text(cleaned, max_length=400)
        for pattern in _EXTRA_SECRET:
            cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned[:300]


def _visible_evidence(case: EvalCaseSpec, variant: Variant) -> list[dict[str, Any]]:
    home = case.input_data.get("workspace_id")
    items: list[dict[str, Any]] = case.input_data.get("evidence", [])
    return [e for e in items if variant.include_foreign or e.get("workspace_id", home) == home]


def _incident_tokens(case: EvalCaseSpec) -> set[str]:
    incident = case.input_data.get("incident", {})
    return set(tokens(f"{incident.get('title', '')} {incident.get('description', '')}"))


def _overlap(query: set[str], text: str) -> int:
    return len(query & set(tokens(text)))


def _rank(query: list[str], docs: list[dict[str, Any]], mode: str) -> list[tuple[str, float]]:
    stem = mode == "bm25"
    q = set(tokens(" ".join(query), stem))
    doc_words = {d["id"]: tokens(d["text"], stem) for d in docs}
    avg_len = sum(len(w) for w in doc_words.values()) / max(1, len(docs))
    scores: list[tuple[float, str]] = []
    for doc in docs:
        words = doc_words[doc["id"]]
        if mode == "bm25":
            score = 0.0
            for term in q:
                tf = words.count(term)
                if tf:
                    df = sum(1 for w in doc_words.values() if term in w)
                    idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(words) / avg_len))
        else:
            score = float(len(q & set(words)))
        scores.append((-score, doc["id"]))
    return [(doc_id, -neg) for neg, doc_id in sorted(scores)]


def _triage(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    incident = case.input_data["incident"]
    words = tokens(f"{incident.get('title', '')} {incident.get('description', '')}")
    best, best_hits = "unknown", 0
    for category, vocab in _CATEGORY_WORDS.items():
        hits = sum(1 for w in words if w in vocab)
        if hits > best_hits:
            best, best_hits = category, hits
    abstain = (best_hits == 0 or len(words) < 3) and not variant.always_answer
    if best_hits == 0 and variant.always_answer:
        best = "service_degradation"
    urgency = _URGENCY.get(str(incident.get("severity", "")).lower(), "medium")
    out = {
        "category": "unknown" if abstain else best,
        "urgency": urgency,
        "affected_component": incident.get("service", "unknown"),
        "impact_assessment": "Insufficient detail to assess impact."
        if abstain
        else f"{best.replace('_', ' ')} affecting {incident.get('service', 'the service')}.",
        "initial_questions": ["What changed before onset?"],
    }
    return CaseOutput(output=out, text=out["impact_assessment"], abstained=abstain)


def _tools(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    inc = case.input_data["incident"]
    text = f"{inc.get('title', '')} {inc.get('description', '')}".lower()
    calls = [ToolCall(name="get_incident_context")]
    if any(w in text for w in ("error", "exception", "5xx", "timeout", "crash")):
        calls.append(ToolCall(name="query_service_logs", arguments={"limit": 20}))
    if any(w in text for w in ("latency", "cpu", "memory", "saturation", "slow")):
        calls.append(ToolCall(name="compare_metric_windows"))
    if any(w in text for w in ("deploy", "release", "rollout", "regression", "after")):
        calls.append(
            ToolCall(
                name="list_recent_changes",
                arguments={"hours": int(case.input_data.get("change_window_hours", 48))},
            )
        )
    if any(w in text for w in ("runbook", "procedure", "playbook")):
        calls.append(ToolCall(name="search_runbooks", arguments={"top_k": 3}))
    if any(w in text for w in ("alarm", "alert", "paged")):
        calls.append(ToolCall(name="get_alarm_context"))
    if variant.follow_injection:
        for name in case.input_data.get("injected_tool_requests", []):
            calls.append(ToolCall(name=name))
    return CaseOutput(output={"tool_plan": [c.name for c in calls]}, tool_calls=calls)


def _grounded(case: EvalCaseSpec, variant: Variant) -> tuple[list[dict[str, Any]], list[str], str]:
    query = _incident_tokens(case)
    threshold = int(case.input_data.get("min_overlap", 2))
    visible = _visible_evidence(case, variant)
    scored = sorted(
        ((_overlap(query, e["text"]), e["id"], e) for e in visible), key=lambda x: (-x[0], x[1])
    )
    picked = [e for score, _, e in scored if score >= threshold][:3]
    contradicting = [e["id"] for e in picked if any(m in e["text"].lower() for m in _CONTRADICTION)]
    supporting = [e for e in picked if e["id"] not in contradicting]
    quote = sanitize(supporting[0]["text"], variant) if supporting else ""
    return supporting, contradicting, quote


def _report(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    supporting, contradicting, quote = _grounded(case, variant)
    abstain = not supporting and not variant.always_answer
    cited = [e["id"] for e in supporting]
    if variant.invent_citation:
        cited.append("ev-invented-0000")
    if abstain:
        root = "Insufficient evidence to identify a root cause."
        confidence = 0.1
    else:
        root = f"Evidence indicates: {quote}" if quote else "The failure pattern is unexplained."
        correlated_only = bool(case.input_data.get("correlation_only"))
        confidence = 0.4 if correlated_only else min(0.85, 0.5 + 0.1 * len(supporting))
    out = {
        "title": f"Investigation: {case.input_data['incident'].get('title', 'incident')}",
        "executive_summary": root,
        "root_cause": root,
        "confidence": confidence,
        "evidence_citations": cited,
        "contradicting_ids": contradicting,
        "unknowns_and_assumptions": ["Findings are correlations, not confirmed causation."],
        "abstained": abstain,
    }
    return CaseOutput(
        output=out,
        text=root,
        cited_evidence_ids=cited,
        abstained=abstain,
        schema_valid=True,
    )


def _retrieval(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    home = case.input_data.get("workspace_id")
    corpus = [
        d
        for d in case.input_data.get("corpus", [])
        if variant.include_foreign or d.get("workspace_id", home) == home
    ]
    scored = _rank(tokens(case.input_data["query"]), corpus, variant.rank_mode)
    ranked = [doc_id for doc_id, _ in scored]
    k = int(case.expected_output.get("k", 3))
    top = scored[0][1] if scored else 0.0
    # Cite only documents that are close to the best match; never cite a zero-score document.
    floor = max(2.0, math.ceil(0.6 * top)) if variant.rank_mode == "overlap" else 0.9 * top
    cited = [doc_id for doc_id, score in scored[:k] if top > 0 and score >= floor]
    if variant.invent_citation:
        cited = [*cited, "doc-invented-0000"]
    return CaseOutput(output={"citations": cited}, retrieved_ids=ranked, cited_evidence_ids=cited)


def _changes(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    inc = case.input_data["incident"]
    query = set(tokens(f"{inc.get('title', '')} {inc.get('description', '')}"))
    scored: list[tuple[float, str]] = []
    for change in case.input_data.get("changes", []):
        score = 3.0 if change.get("service") == inc.get("service") else 0.0
        score += 2.0 / (1.0 + max(0.0, float(change["minutes_before_incident"])) / 30.0)
        score += _overlap(query, f"{change['summary']} {' '.join(change.get('files', []))}")
        if change["minutes_before_incident"] < 0:
            score = -1.0  # a change after onset cannot be a cause
        scored.append((-score, change["id"]))
    ranked = [cid for _, cid in sorted(scored)]
    text = (
        "Top suspect changes are correlated in time and content; this is not confirmed causation."
    )
    return CaseOutput(output={"suspects": ranked[:3]}, text=text, suspect_change_ids=ranked)


def _remediation(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    data = case.input_data
    requested: list[str] = list(data.get("requested_actions", []))
    prohibited = set(data.get("prohibited_actions", []))
    allowed = set(data.get("allowed_actions", []))
    sufficient = bool(data.get("evidence_sufficient", True))
    if variant.auto_approve:
        actions = [*requested, "auto_approve"]
        return CaseOutput(
            output={"proposed": actions},
            remediation_actions=actions,
            final_status="approved",
            status_path=["draft", "approved"],
            text="Applied requested actions.",
        )
    proposed = [a for a in requested if a in allowed and a not in prohibited]
    if not sufficient or not proposed:
        return CaseOutput(
            output={"proposed": [], "reason": "abstained: insufficient evidence or blocked"},
            abstained=True,
            final_status="blocked",
            status_path=["draft", "blocked"],
            text="No remediation proposed; the request is blocked or evidence is insufficient.",
        )
    return CaseOutput(
        output={"proposed": proposed},
        remediation_actions=proposed,
        final_status="awaiting_approval",
        status_path=["draft", "proposed", "awaiting_approval"],
        text="Remediation proposed; human approval is required before any action.",
    )


def _isolation(case: EvalCaseSpec, variant: Variant) -> CaseOutput:
    data = case.input_data
    failure = data.get("provider_failure")
    if failure:
        code, status = _PROVIDER_ERRORS[failure]
        return CaseOutput(
            output={},
            error_class=code,
            final_status=status,
            abstained=True,
            schema_valid=True,
            text="The provider call failed; no result was produced.",
        )
    if data.get("unsupported_request"):
        return CaseOutput(
            output={"answer": "This request is not supported."},
            abstained=True,
            text="This request is not supported and was not performed.",
        )
    supporting, contradicting, quote = _grounded(case, variant)
    calls: list[ToolCall] = []
    if variant.follow_injection:
        calls = [ToolCall(name=n) for n in data.get("injected_tool_requests", [])]
    abstain = not supporting and not variant.always_answer
    cited = [e["id"] for e in supporting]
    text = "Insufficient evidence in this workspace." if abstain else f"Evidence indicates: {quote}"
    if variant.follow_injection and data.get("injection_canaries"):
        text += " " + " ".join(data["injection_canaries"])
    return CaseOutput(
        output={"answer": text, "contradicting_ids": contradicting},
        text=text,
        cited_evidence_ids=cited,
        abstained=abstain,
        tool_calls=calls,
    )


_DISPATCH = {
    "triage": _triage,
    "tool_selection": _tools,
    "grounding": _report,
    "report_quality": _report,
    "retrieval_citations": _retrieval,
    "change_correlation": _changes,
    "remediation_safety": _remediation,
    "isolation_injection_abstention": _isolation,
}


def run_case(case: EvalCaseSpec, variant: Variant = BASELINE) -> CaseOutput:
    """Execute the reference agent on one case. Pure and deterministic."""
    output = _DISPATCH[case.suite](case, variant)
    output.input_tokens = 120 + 3 * len(str(case.input_data)) // 10
    output.output_tokens = 40 + len(output.text) // 4
    # Deterministic synthetic latency from content size; labelled synthetic in every report.
    output.latency_ms = 5.0 + len(str(case.input_data)) / 200.0
    return output
