"""Durable LangGraph remediation workflow, executed only by RQ workers.

Nodes delegate to :class:`RemediationPipeline` steps (idempotent, DB-backed). The graph pauses at
``await_approval`` with ``interrupt``; the review API records the human decision and resumes it.
Publication is a separate RQ job (``publish``) that re-validates the approval binding.
"""

import logging
from typing import Any, Literal, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.orm import Session

from opspilot_api.config import Settings
from opspilot_api.remediation.pipeline import (
    RemediationCancelledError,
    RemediationFailureError,
    RemediationPipeline,
)

logger = logging.getLogger(__name__)
Entry = Literal["start", "reverify"]


class RemediationState(TypedDict, total=False):
    remediation_id: str
    workspace_id: str
    entry: str
    halt: bool
    feedback: str | None
    review: dict[str, Any]


def _ids(state: RemediationState) -> tuple[UUID, UUID]:
    return UUID(state["workspace_id"]), UUID(state["remediation_id"])


def build_graph(pipeline: RemediationPipeline, checkpointer: PostgresSaver | None) -> Any:
    def node(name: str) -> Any:
        def run(state: RemediationState) -> RemediationState:
            workspace_id, remediation_id = _ids(state)
            step = getattr(pipeline, name)
            return cast(RemediationState, step(workspace_id, remediation_id))

        run.__name__ = name
        return run

    def generate_patch(state: RemediationState) -> RemediationState:
        workspace_id, remediation_id = _ids(state)
        return cast(
            RemediationState,
            pipeline.generate_patch(workspace_id, remediation_id, state.get("feedback")),
        )

    def await_approval(state: RemediationState) -> RemediationState:
        workspace_id, remediation_id = _ids(state)
        decision = interrupt({"remediation_id": str(remediation_id), "awaiting": "human_approval"})
        review = dict(decision) if isinstance(decision, dict) else {}
        action = review.get("action")
        # The API has already applied the decision transactionally; the graph only routes.
        feedback = review.get("comment") if action == "revision_requested" else None
        pipeline.purge(remediation_id)
        del workspace_id
        return {"review": review, "feedback": feedback, "halt": action != "revision_requested"}

    def route_entry(state: RemediationState) -> str:
        return "policy_check" if state.get("entry") == "reverify" else "load_context"

    def after(next_node: str) -> Any:
        def route(state: RemediationState) -> str:
            return END if state.get("halt") else next_node

        return route

    graph = StateGraph(RemediationState)
    chain = [
        ("load_context", "select_repository"),
        ("select_repository", "select_code_context"),
        ("select_code_context", "generate_patch"),
        ("generate_patch", "policy_check"),
        ("policy_check", "prepare_sandbox"),
        ("prepare_sandbox", "baseline_checks"),
        ("baseline_checks", "patched_checks"),
        ("patched_checks", "compare_results"),
        ("compare_results", "verification_report"),
        ("verification_report", "await_approval"),
    ]
    for name in (
        "load_context",
        "select_repository",
        "select_code_context",
        "policy_check",
        "prepare_sandbox",
        "baseline_checks",
        "patched_checks",
        "compare_results",
        "verification_report",
    ):
        graph.add_node(name, node(name))
    graph.add_node("generate_patch", generate_patch)
    graph.add_node("await_approval", await_approval)
    graph.add_conditional_edges(START, route_entry, ["load_context", "policy_check"])
    for source, target in chain:
        graph.add_conditional_edges(source, after(target), [target, END])
    # A revision request loops back to patch generation with the reviewer's feedback.
    graph.add_conditional_edges(
        "await_approval",
        lambda s: END if s.get("halt") else "generate_patch",
        ["generate_patch", END],
    )
    return graph.compile(checkpointer=checkpointer)


def run_remediation_workflow(
    settings: Settings,
    workspace_id: UUID,
    remediation_id: UUID,
    thread_id: str,
    entry: Entry = "start",
    resume: dict[str, Any] | None = None,
    pipeline: RemediationPipeline | None = None,
) -> None:
    owned = pipeline is None
    runtime = pipeline or RemediationPipeline(settings)
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    try:
        with PostgresSaver.from_conn_string(settings.checkpoint_url) as checkpointer:
            checkpointer.setup()
            graph = build_graph(runtime, checkpointer)
            if resume is not None and not graph.get_state(config).next:
                return  # nothing is waiting: an earlier delivery already resumed this thread
            exists = checkpointer.get(config) is not None
            initial: RemediationState | None = None
            if resume is None and not exists:
                initial = {
                    "remediation_id": str(remediation_id),
                    "workspace_id": str(workspace_id),
                    "entry": entry,
                }
            graph.invoke(Command(resume=resume) if resume is not None else initial, config)
    except RemediationCancelledError:
        return
    except Exception as exc:
        _mark_failed(runtime, workspace_id, remediation_id, exc)
        raise
    finally:
        if owned:
            runtime.close()


def _mark_failed(
    runtime: RemediationPipeline, workspace_id: UUID, remediation_id: UUID, exc: BaseException
) -> None:
    code = getattr(exc, "code", type(exc).__name__.lower())
    try:
        with Session(runtime.engine) as session:
            proposal = runtime.load(session, workspace_id, remediation_id)
            runtime.fail(session, proposal, str(code)[:80], f"{type(exc).__name__}: {exc}")
    except (RemediationCancelledError, RemediationFailureError, LookupError):
        return
    except Exception as inner:
        logger.error(
            "could not record remediation failure", extra={"error_class": type(inner).__name__}
        )
