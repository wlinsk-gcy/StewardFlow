from __future__ import annotations

from datetime import datetime

COMPACTION_SYSTEM_PROMPT = """You are StewardFlow's context compaction assistant.

Your only job is to summarize the conversation so the agent can continue the work after context compaction.

Do not answer questions, do not propose new work beyond the summary, and do not pretend to execute tools.
Only output the summary."""

WHAT_DID_WE_DO_PROMPT = "What did we do so far?"
CONTINUE_PROMPT = "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed."


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
