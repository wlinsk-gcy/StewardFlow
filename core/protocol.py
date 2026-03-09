from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

from utils.id_util import get_sonyflake


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class RunAgentRequest(BaseModel):
    client_id: str
    task: str
    trace_id: Optional[str] = None


class RunAgentResponse(BaseModel):
    trace_id: Optional[str] = None
    status: Optional[str] = None
    request_id: Optional[str] = None
    message: Optional[str] = None


class StopAgentRequest(BaseModel):
    trace_id: str


class StopAgentResponse(BaseModel):
    trace_id: str
    status: str
    message: Optional[str] = None


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeType(str, Enum):
    THINK = "think"
    DECIDE = "decide"
    EXECUTE = "execute"
    GUARD = "guard"
    HITL = "hitl"
    OBSERVE = "observe"
    END = "end"


class EventType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    FINAL = "final"
    SCREENSHOT = "screenshot"
    TOKEN_INFO = "token_info"
    HITL_REQUEST = "hitl_request"
    HITL_CONFIRM = "hitl_confirm"
    ANSWER = "answer"
    END = "end"
    ERROR = "error"


@dataclass
class Event:
    event_type: EventType
    agent_id: str = ""
    msg_id: str = ""
    data: Optional[dict] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "agent_id": self.agent_id,
            "msg_id": self.msg_id,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }


class ActionType(str, Enum):
    TOOL = "tool"
    REQUEST_INPUT = "request_input"
    REQUEST_CONFIRM = "request_confirm"
    FINISH = "finish"
    ERROR = "error"


@dataclass
class HitlTicket:
    ticket_id: str = field(default_factory=lambda: get_sonyflake("hitl_"))
    kind: Literal["tool_confirm", "request_input", "request_confirm"] = "tool_confirm"
    status: Literal["open", "resolved", "cancelled"] = "open"
    turn_id: Optional[str] = None
    step_id: Optional[str] = None
    action_id: Optional[str] = None
    request_id: Optional[str] = None
    prompt: Optional[str] = None
    decision: Optional[Literal["approved", "denied"]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "kind": self.kind,
            "status": self.status,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "action_id": self.action_id,
            "request_id": self.request_id,
            "prompt": self.prompt,
            "decision": self.decision,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "resolved_at": self.resolved_at.isoformat()
            if isinstance(self.resolved_at, datetime) and self.resolved_at
            else self.resolved_at,
        }


@dataclass
class Trace:
    client_id: str
    trace_id: str = field(default_factory=lambda: get_sonyflake("trace_"))
    status: AgentStatus = AgentStatus.RUNNING
    node: Optional[NodeType] = None
    current_turn_id: Optional[str] = None
    current_step_id: Optional[str] = None
    pending_action_id: Optional[str] = None
    hitl_ticket: Optional[HitlTicket] = None
    turns: List["Turn"] = field(default_factory=list)
    max_turns: int = 100
    token_info: Optional[Dict[str, Any]] = field(default_factory=dict)
    error_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "trace_id": self.trace_id,
            "status": _enum_value(self.status),
            "node": _enum_value(self.node),
            "current_turn_id": self.current_turn_id,
            "current_step_id": self.current_step_id,
            "pending_action_id": self.pending_action_id,
            "hitl_ticket": self.hitl_ticket.to_dict() if self.hitl_ticket else None,
            "turns": [turn.to_dict() for turn in self.turns],
            "max_turns": self.max_turns,
            "token_info": self.token_info,
            "error_count": self.error_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TurnStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Turn:
    index: int
    user_input: str
    turn_id: str = field(default_factory=lambda: get_sonyflake("turn_"))
    status: TurnStatus = TurnStatus.RUNNING
    steps: List["Step"] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "user_input": self.user_input,
            "turn_id": self.turn_id,
            "status": _enum_value(self.status),
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class StepStatus(str, Enum):
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRM = "waiting_confirm"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Step:
    index: int
    status: StepStatus = StepStatus.RUNNING
    step_id: str = field(default_factory=lambda: get_sonyflake("step_"))
    thought: Optional[str] = None
    actions: List["Action"] = field(default_factory=list)
    observations: List["Observation"] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": _enum_value(self.status),
            "step_id": self.step_id,
            "thought": self.thought,
            "actions": [action.to_dict() for action in self.actions],
            "observations": [observation.to_dict() for observation in self.observations],
            "tool_calls": self.tool_calls,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class ActionStatus(str, Enum):
    PLANNED = "planned"
    WAITING_CONFIRM = "waiting_confirm"
    WAITING_INPUT = "waiting_input"
    APPROVED = "approved"
    DENIED = "denied"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class Action:
    action_id: str
    type: ActionType
    tool_name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    request_input: Optional[str] = None
    full_ref: Optional[str] = None
    requires_confirm: bool = False
    confirm_status: Optional[Literal["pending", "approved", "denied"]] = None
    status: ActionStatus = ActionStatus.PLANNED
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "type": _enum_value(self.type),
            "tool_name": self.tool_name,
            "args": self.args,
            "message": self.message,
            "request_input": self.request_input,
            "full_ref": self.full_ref,
            "requires_confirm": self.requires_confirm,
            "confirm_status": self.confirm_status,
            "status": _enum_value(self.status),
            "error": self.error,
        }


class ObservationType(str, Enum):
    TOOL_RESULT = "tool_result"
    HITL_DENIED = "hitl_denied"
    TOOL_ERROR = "tool_error"
    INFO = "info"


@dataclass
class Observation:
    observation_id: str
    action_id: str
    type: ObservationType
    ok: bool
    content: str
    metadata: Optional[Dict[str, Any]] = None
    full_ref: Optional[Dict[str, Any]] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "type": _enum_value(self.type),
            "ok": self.ok,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
