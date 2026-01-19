from contextvars import ContextVar

# 当在 asyncio 任务中切换执行流时，ContextVar 自动继承父上下文，无需手动传递。
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
agent_id_ctx   = ContextVar("agent_id", default="-")