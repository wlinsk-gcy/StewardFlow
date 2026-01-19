"""
Agent 协议定义
包含所有核心数据结构：AgentState、Thought、Action、Observation、HITLRequest 等
"""

from dataclasses import dataclass, field
from typing import Optional, Any, List, Dict
from enum import Enum
from datetime import datetime
import uuid
from utils.id_util import get_sonyflake
from pydantic import BaseModel


class RunAgentRequest(BaseModel):
    """运行 Agent 请求"""
    client_id: str
    task: str
    agent_id: str = None


class RunAgentResponse(BaseModel):

    agent_id: Optional[str] = None
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
    turn_id: str = ""

    # 携带数据
    data: Optional[dict] = None

    # 时间戳
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self):
        return {
            # "event_id": self.event_id,
            "event_type": self.event_type.value,
            "agent_id": self.agent_id,
            "turn_id": self.turn_id,
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
    scenario: Optional[List[Dict[str, Any]]] = None # 预设场景

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
            "error_message": self.error_message
        }
