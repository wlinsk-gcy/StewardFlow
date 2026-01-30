"""
Agent 协议定义
包含所有核心数据结构：AgentState、Thought、Action、Observation、HITLRequest 等
"""

from dataclasses import dataclass, field
from typing import Optional, Any, List, Dict, Literal
from enum import Enum
from datetime import datetime
import uuid
from utils.id_util import get_sonyflake
from pydantic import BaseModel


class RunAgentRequest(BaseModel):
    """运行 Agent 请求"""
    client_id: str
    task: str
    trace_id: str = None


class RunAgentResponse(BaseModel):

    trace_id: Optional[str] = None
    status: Optional[str] = None
    request_id: Optional[str] = None
    message: Optional[str] = None

# Agent的生命周期
class AgentStatus(str, Enum):
    IDLE = "idle"        # 未启动
    RUNNING = "running" # 可调度
    WAITING = "waiting" # 等外部事件（HITL）
    PAUSED = "paused"   # 人工暂停
    DONE = "done"       # 正常完成
    FAILED = "failed"   # 异常终止

# Node节点的运行状态
# 流程走到哪一步，不是靠if / while，而是current_node在哪里？
class NodeType(str, Enum):
    THINK = "think"
    DECIDE = "decide"
    EXECUTE = "execute"
    HITL = "hitl" # request or confirm ?
    OBSERVE = "observe"
    END = "end"


class EventType(str, Enum):
    """事件类型"""
    THOUGHT = "thought"           # execution trace - 展示思考过程
    ACTION = "action"             # execution trace - 展示行为动作
    OBSERVATION = "observation"   # execution trace - 展示执行结果
    FINAL = "final"               # execution trace - 展示最终回复
    SCREENSHOT = "screenshot"     # browser view - base64 screenshot
    TOKEN_INFO = "token_info"     # token消耗详情

    HITL_REQUEST = "hitl_request" # 当需要HITL用户输入时，向用户输出的提示词
    HITL_CONFIRM = "hitl_confirm" # 当需要HITL用户确认时

    ANSWER = "answer"             # 模型的回答
    END = "end"                   # 结束标记
    ERROR = "error"               # 异常标记

@dataclass
class Event:
    """状态变更事件"""
    event_type: EventType = None

    agent_id: str = ""
    msg_id: str = ""

    # 携带数据
    data: Optional[dict] = None

    # 时间戳
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self):
        return {
            "event_type": self.event_type.value,
            "agent_id": self.agent_id,
            "msg_id": self.msg_id,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),  # datetime -> str
        }


class ActionType(str, Enum):
    """Action 类型"""
    # 常规工具
    TOOL = "tool"
    # HITL 相关
    REQUEST_INPUT = "request_input"    # 请求用户输入
    REQUEST_CONFIRM = "request_confirm"  # 请求确认下一步
    # 结束相关
    FINISH = "finish"                  # 任务完成
    ERROR = "error"                    # 发生错误


class HITLRequestType(str, Enum):
    """HITL 请求类型"""
    USER_INPUT = "user_input"        # 自由文本输入
    CONFIRM_ACTION = "confirm_action"  # 确认/拒绝 Action
    SELECT_OPTION = "select_option"  # 选项选择


@dataclass
class Thought:
    """Agent 的思考内容"""
    content: str                     # 思考的文本描述
    turn_id: str                     # 所属的轮次 ID
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self):
        return {
            "content": self.content,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),  # datetime -> str
        }


@dataclass
class Action:
    """Agent 决定的行动"""
    type: ActionType                        # Action 类型
    tool_name: Optional[str] = None         # 使用的工具名称
    args: dict = field(default_factory=dict)  # 工具参数
    thought: Optional[str] = None           # 关联的思考（可选）
    turn_id: str = ""                       # 所属的轮次 ID
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # HITL 特定字段
    prompt: Optional[str] = None            # 提示语
    # requires_confirmation: bool = False     # 是否需要确认

    answer: Optional[str] = None            # type为finish时的最终回答

    def to_dict(self) -> dict:
        """转换为字典（用于序列化）"""
        return {
            "type": self.type.value,
            "tool_name": self.tool_name,
            "args": self.args,
            "thought": self.thought,
            "prompt": self.prompt,
            "answer": self.answer,
            # "requires_confirmation": self.requires_confirmation
        }


@dataclass
class Observation:
    """Action 执行结果"""
    content: str                     # 结果文本
    turn_id: str                     # 所属的轮次 ID
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # 执行状态
    success: bool = True            # 是否成功
    error: Optional[str] = None     # 错误信息（如果有）

    # HITL 特定字段
    human_input: Optional[str] = None  # 人工输入（如果是 HITL）
    role: str = "user"
    tool_call_id: Optional[str] = None


    def to_dict(self) -> dict:
        """转换为字典（用于序列化）"""
        return {
            "role": self.role,
            "tool_call_id": self.tool_call_id,
            "content": self.content,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "success": self.success,
            "error": self.error,
            "human_input": self.human_input,
        }

@dataclass(frozen=True)
class HITLRequest:
    """HITL 人工介入请求"""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    request_type: HITLRequestType = HITLRequestType.USER_INPUT

    # 请求内容
    prompt: str = ""                 # 提示语
    context: Optional[str] = None    # 上下文信息

    # 选项（用于 SELECT_OPTION）
    options: List[str] = field(default_factory=list)

    # 默认值
    default_value: Optional[str] = None

    # 是否必填
    required: bool = True

    # 关联信息
    turn_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        """转换为字典（用于序列化）"""
        return {
            "request_id": self.request_id,
            "request_type": self.request_type.value,
            "prompt": self.prompt,
            "context": self.context,
            "options": self.options,
            "required": self.required
        }

class HITLResponse(BaseModel):
    """HITL 响应"""
    request_id: Optional[str] = None
    status: str  # accepted / rejected
    message: Optional[str] = None


@dataclass
class HITLResult:
    """HITL 处理结果"""
    success: bool
    response: Optional[HITLResponse] = None

# @dataclass(frozen=True)
@dataclass
class Pending:
    """当前未完成的一步"""
    thought: Thought = field(default_factory=Thought)
    action: Action = field(default_factory=Action)
    requires_hitl: bool = False
    confirmed: bool = False

@dataclass
class AgentState:
    """Agent 可恢复运行状态（Checkpoint）"""
    client_id: str
    # ========== 身份 ==========
    agent_id: str = field(default_factory=lambda: get_sonyflake())

    # ========== Runtime 层 ==========
    # Agent的生命周期
    status: AgentStatus = AgentStatus.IDLE  # IDLE / RUNNING / WAITING / PAUSED / DONE / FAILED
    # Agent当前运行到哪个节点？
    current_node: Optional[NodeType] = None  # THINK / EXECUTE / HITL / OBSERVE / END

    # ========== 任务上下文 ==========
    task: str = "" # 当前任务描述

    max_turns: int = 50  # 最大轮次限制
    current_turn: int = 0  # 当前轮次

    # ========== 执行历史（已提交，不可变） ==========
    tao_trajectory: List[Dict[str, Any]] = field(default_factory=list)
    # 每一项：
    # {
    #   "thought": Thought,
    #   "action": Action,
    #   "observation": Observation,
    #   "timestamp": ...
    # }

    # ========== 当前未完成一步（关键） ==========
    pending: Optional[Pending] = None
    # {
    #     thought: Thought,
    #     action: Action,
    #     requires_hitl: bool = False,
    #     confirmed: bool = False
    # }

    # ========== HITL 状态 ==========
    hitl: Optional[HITLRequest] = None  # 待处理的 HITL 请求
    hitl_count: int = 0                 # HITL 介入次数统计

    # ========== 元数据 ==========
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    error_count: int = 0

    token_info: Optional[Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为字典（用于 API 响应）"""
        return {
            "client_id": self.client_id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "current_node": self.current_node.value if self.current_node else None,
            "task": self.task,
            "current_turn": self.current_turn,
            "max_turns": self.max_turns,
            "tao_trajectory": self.tao_trajectory,
            "pending": self.pending if self.pending else None,
            "hitl": self.hitl.to_dict() if self.hitl else None,
            "hitl_count": self.hitl_count,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "error_message": self.error_message,
            "token_info": self.token_info,
        }



@dataclass
class Trace:
    """
    会话容器
    """
    client_id: str
    trace_id: str = field(default_factory=lambda: get_sonyflake("trace_"))

    status: AgentStatus = AgentStatus.RUNNING  # IDLE / RUNNING / WAITING / PAUSED / DONE / FAILED
    node: Optional[NodeType] = None # THINK / DECIDE / EXECUTE / HITL / OBSERVE / END

    # 指针（恢复时只要靠它定位到“正在进行的 turn/step/action”）
    current_turn_id: Optional[str] = None
    current_step_id: Optional[str] = None
    pending_action_id: Optional[str] = None  # 等 confirm 或正在执行的 action

    turns: List["Turn"] = field(default_factory=list)
    max_turns: int = 100

    token_info: Optional[Dict[str, Any]] = field(default_factory=dict)
    error_count: int = 0
    error_message: Optional[str] = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self):
        return {
            "client_id": self.client_id,
            "trace_id": self.trace_id,
            "status": self.status.value,
            "node": self.node.value,
            "current_turn_id": self.current_turn_id,
            "current_step_id": self.current_step_id,
            "pending_action_id": self.pending_action_id,
            "turns": [turn.to_dict() for turn in self.turns],
            "max_turns": self.max_turns,
            "token_info": self.token_info,
            "error_count": self.error_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class TurnStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

@dataclass
class Turn:
    """
    用户输入容器
    """
    index: int
    user_input: str  # 用户原话（补材料也在这里）
    turn_id: str = field(default_factory=lambda: get_sonyflake("turn_"))

    status: TurnStatus = TurnStatus.RUNNING

    # 一个 turn 内可能会有多个 step（多次 LLM 规划 + 工具 + 回灌）
    steps: List["Step"] = field(default_factory=list)

    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None

    def to_dict(self):
        return {
            "index": self.index,
            "user_input": self.user_input,
            "turn_id": self.turn_id,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class StepStatus(str, Enum):
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_CONFIRM = "waiting_confirm"
    DONE = "done"
    FAILED = "failed"

@dataclass
class Step:
    """
    LLM规划容器
    """
    index: int
    status: StepStatus = StepStatus.RUNNING
    step_id: str = field(default_factory=lambda: get_sonyflake("step_"))

    thought: Optional[str] = None  # 记录 reasoning_content（内部，不进 messages）

    actions: List["ActionV2"] = field(default_factory=list)  # LLM 输出
    observations: List["ObservationV2"] = field(default_factory=list)  # 执行产物（按 action_id 对齐） # 没有tool执行时为null是对的

    tool_calls: List[Dict[str, Any]] = field(default_factory=list) # LLM原样输出的tool_calls

    created_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    def to_dict(self):
        return {
            "index": self.index,
            "status": self.status.value,
            "step_id": self.step_id,
            "thought": self.thought,
            "actions": [action.to_dict() for action in self.actions],
            "observations": [observation.to_dict() for observation in self.observations] if self.observations else [],
            "tool_calls": self.tool_calls,
            "created_at": self.created_at.isoformat(),
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

@dataclass
class ActionV2:
    action_id: str
    type: ActionType

    # tool
    tool_name: Optional[str] = None
    args: Optional[Dict[str, Any]] = None

    message: Optional[str] = None
    # type='request_input' -> 用户补充的信息
    request_input: Optional[str] = None

    # assistant原始引用
    full_ref: Optional[str] = None

    # HITL（只对 tool 有意义）
    requires_confirm: bool = False
    # confirm_request_id: Optional[str] = None  # 前端确认事件 id
    confirm_status: Optional[Literal["pending", "approved", "denied"]] = None
    # confirm_note: Optional[str] = None  # 用户拒绝/备注/修改原因
    # args_edited_by_user: Optional[Dict[str, Any]] = None  # 用户修改后的 args（如允许）

    status: ActionStatus = ActionStatus.PLANNED
    error: Optional[str] = None

    def to_dict(self):
        return {
            "action_id": self.action_id,
            "type": self.type.value,
            "tool_name": self.tool_name,
            "args": self.args,
            "message": self.message,
            "request_input": self.request_input,
            "full_ref": self.full_ref,
            "requires_confirm": self.requires_confirm,
            "confirm_status": self.confirm_status,
            "status": self.status.value,
            "error": self.error,
        }


class ObservationType(str, Enum):
    TOOL_RESULT = "tool_result" # 工具执行成功/有有效返回
    HITL_DENIED = "hitl_denied" # 人拒绝/未授权导致工具未执行
    TOOL_ERROR = "tool_error" # 工具执行失败/异常/超时
    INFO = "info" # 非工具执行结果的“系统信息/中间事件”

@dataclass
class ObservationV2:
    observation_id: str
    action_id: str
    type: ObservationType

    ok: bool
    content: Any  # 建议存 compact 结果（长结果落盘用 ref）
    full_ref: Optional[Dict[str, Any]] = None  # 可选：{store:"blob", key:"..."}
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self):
        return {
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "type": self.type.value,
            "ok": self.ok,
            "content": self.content,
            "full_ref": self.full_ref,
            "created_at": self.created_at.isoformat(),  # datetime -> str
        }
