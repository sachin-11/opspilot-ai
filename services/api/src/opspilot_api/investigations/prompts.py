"""Versioned prompt registry. Prompt text is never persisted in investigation rows."""

from dataclasses import dataclass

from pydantic import BaseModel

from opspilot_api.investigations.safety import SYSTEM_SAFETY_INSTRUCTION
from opspilot_api.investigations.schemas import (
    CritiqueResult,
    HypothesisSet,
    InvestigationPlan,
    InvestigationReport,
    TriageResult,
)

PROMPT_VERSION = "investigation-v1.0.0"


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    version: str
    instruction: str
    output_schema: type[BaseModel]
    change_notes: str


PROMPTS: dict[str, PromptDefinition] = {
    "triage": PromptDefinition(
        "triage",
        PROMPT_VERSION,
        "Classify impact and identify bounded evidence needs.",
        TriageResult,
        "Initial Module 2 prompt.",
    ),
    "plan": PromptDefinition(
        "plan",
        PROMPT_VERSION,
        "Select only registered read-only investigation tools.",
        InvestigationPlan,
        "Initial Module 2 prompt.",
    ),
    "hypotheses": PromptDefinition(
        "hypotheses",
        PROMPT_VERSION,
        "Rank bounded hypotheses and cite evidence UUIDs.",
        HypothesisSet,
        "Initial Module 2 prompt.",
    ),
    "critique": PromptDefinition(
        "critique",
        PROMPT_VERSION,
        "Challenge unsupported claims and abstain when evidence is insufficient.",
        CritiqueResult,
        "Initial Module 2 prompt.",
    ),
    "report": PromptDefinition(
        "report",
        PROMPT_VERSION,
        "Produce an advisory report with evidence citations and no production actions.",
        InvestigationReport,
        "Initial Module 2 prompt.",
    ),
    "revision": PromptDefinition(
        "revision",
        PROMPT_VERSION,
        "Revise only the report using reviewer feedback and existing evidence.",
        InvestigationReport,
        "Initial Module 2 prompt.",
    ),
}


def system_prompt(name: str) -> str:
    definition = PROMPTS[name]
    return (
        f"{SYSTEM_SAFETY_INSTRUCTION}\n\nTask: {definition.instruction}\n"
        f"Output must match {definition.output_schema.__name__}."
    )
