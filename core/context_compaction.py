from __future__ import annotations

from datetime import datetime
from typing import Any

COMPACTION_SYSTEM_PROMPT = """You are StewardFlow's context compaction assistant.

Your only job is to summarize the conversation so the agent can continue the work after context compaction.

Do not answer questions, do not propose new work beyond the summary, and do not pretend to execute tools.
Only output the summary."""

WHAT_DID_WE_DO_PROMPT = "What did we do so far?"
CONTINUE_PROMPT = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."
BOUNDARY_BEFORE_FIRST_STEP = "__before_first_step__"


def build_summary_instruction_prompt() -> str:
    return """Provide a detailed prompt for continuing the conversation above.
Focus on information that would be helpful for continuing the work, including what was done, what is currently being worked on, which files, tools, or pages are relevant, and what should happen next.

This summary will be used after context compaction so the agent can continue the work without the full original conversation.

When constructing the summary, try to stick to this template:
---
## Goal

[What goal(s) is the user trying to accomplish?]

## Instructions

- [Important instructions, constraints, or preferences from the user]
- [If there is a plan or spec, include the information needed to continue it]

## Discoveries

[What notable things were learned that will matter for continuing the work]

## Accomplished

[What has been completed, what is in progress, and what remains]

## Relevant files / directories / tools / pages

[Structured list of relevant files, directories, tools, URLs, pages, or environments involved in the task]
---"""


def make_pending_compaction(*, overflow: bool, source: str, turn_id: str, step_id: str | None) -> dict:
    return {
        "overflow": bool(overflow),
        "source": str(source or "").strip(),
        "trigger_turn_id": str(turn_id or "").strip(),
        "trigger_step_id": str(step_id or "").strip() or None,
        "created_at": datetime.utcnow().isoformat(),
    }


def make_context_compaction(
    *,
    summary_text: str,
    boundary_turn_id: str,
    boundary_step_id: str | None,
    resume_prompt: str,
    mode: str,
    source: str,
    model: str | None,
) -> dict:
    return {
        "summary_text": str(summary_text or "").strip(),
        "boundary_turn_id": str(boundary_turn_id or "").strip(),
        "boundary_step_id": str(boundary_step_id or "").strip() or None,
        "resume_prompt": str(resume_prompt or "").strip(),
        "mode": str(mode or "").strip(),
        "source": str(source or "").strip(),
        "model": str(model or "").strip() or None,
        "created_at": datetime.utcnow().isoformat(),
    }


def get_active_compaction(trace: Any) -> dict[str, Any] | None:
    value = getattr(trace, "context_compaction", None)
    if not isinstance(value, dict):
        return None
    if not value.get("summary_text"):
        return None
    if not value.get("boundary_turn_id"):
        return None
    return value


def get_compaction_boundary(trace: Any) -> tuple[str | None, str | None]:
    compaction = get_active_compaction(trace)
    if not compaction:
        return None, None
    boundary_turn_id = str(compaction.get("boundary_turn_id") or "").strip() or None
    boundary_step_id = str(compaction.get("boundary_step_id") or "").strip() or None
    return boundary_turn_id, boundary_step_id


def resolve_compaction_boundary(trace: Any) -> tuple[int | None, int | None]:
    boundary_turn_id, boundary_step_id = get_compaction_boundary(trace)
    if not boundary_turn_id:
        return None, None

    boundary_turn_index: int | None = None
    boundary_step_index: int | None = None
    for turn_index, turn in enumerate(getattr(trace, "turns", []) or []):
        if getattr(turn, "turn_id", None) != boundary_turn_id:
            continue
        boundary_turn_index = turn_index
        if boundary_step_id == BOUNDARY_BEFORE_FIRST_STEP:
            boundary_step_index = -1
            break
        if not boundary_step_id:
            break
        for step_index, step in enumerate(getattr(turn, "steps", []) or []):
            if getattr(step, "step_id", None) == boundary_step_id:
                boundary_step_index = step_index
                break
        break
    return boundary_turn_index, boundary_step_index


def should_skip_turn_step(
    turn_index: int,
    step_index: int | None,
    boundary_turn_index: int | None,
    boundary_step_index: int | None,
) -> bool:
    if boundary_turn_index is None:
        return False
    if turn_index < boundary_turn_index:
        return True
    if turn_index > boundary_turn_index:
        return False
    if step_index is None:
        return True
    if boundary_step_index is None:
        return True
    return step_index <= boundary_step_index
