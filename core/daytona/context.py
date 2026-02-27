from contextvars import ContextVar

daytona_trace_id_ctx: ContextVar[str | None] = ContextVar("daytona_trace_id", default=None)


def get_current_trace_id() -> str:
    trace_id = daytona_trace_id_ctx.get()
    if not trace_id:
        raise RuntimeError("Daytona tools require trace context, but trace_id is missing")
    return trace_id
