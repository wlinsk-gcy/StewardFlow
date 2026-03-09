import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Optional

from core.cache_manager import CacheManager
from core.executor import TaskExecutor
from core.llm import Provider
from core.protocol import Action, AgentStatus, HitlTicket, NodeType, Observation, Step, Trace, Turn
from core.storage.checkpoint import CheckpointStore
from core.tools.tool import ToolRegistry
from ws.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)


@dataclass
class QueueAdmission:
    queue_length: int
    wait_ms: int


class QueueRejectedError(Exception):
    def __init__(self, reason: str, *, queue_length: int, wait_ms: int = 0) -> None:
        self.reason = str(reason)
        self.queue_length = int(queue_length)
        self.wait_ms = int(wait_ms)
        super().__init__(f"{self.reason}: queue_length={self.queue_length} wait_ms={self.wait_ms}")


class TaskService:
    def __init__(
        self,
        checkpoint: CheckpointStore,
        provider: Provider,
        tool_registry: ToolRegistry,
        ws_manager: ConnectionManager,
        cache_manager: CacheManager,
    ):
        self.checkpoint = checkpoint
        self.provider = provider
        self.tool_registry = tool_registry
        self.ws_manager = ws_manager
        self.cache_manager = cache_manager

        self.queue_lanes_enabled = True
        self.global_concurrency_limit = 4
        self.global_queue_max = 128
        self.queue_wait_timeout_ms = 15000

        self._run_limiter = asyncio.Semaphore(self.global_concurrency_limit)
        self._queue_counter_lock = asyncio.Lock()
        self._queued_tasks: int = 0
        self._trace_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}

        self.executor = TaskExecutor(
            checkpoint,
            provider,
            tool_registry,
            ws_manager,
            cache_manager,
        )

    @staticmethod
    def _attach_background_log(task: asyncio.Task, *, trace_id: str) -> None:
        def _callback(done: asyncio.Task) -> None:
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                logger.warning("background run cancelled: trace=%s", trace_id)
                return
            if exc:
                logger.exception("background run failed: trace=%s err=%s", trace_id, exc)

        task.add_done_callback(_callback)

    def _track_active_task(self, trace_id: str, task: asyncio.Task) -> None:
        self._active_tasks[trace_id] = task

        def _cleanup(done: asyncio.Task) -> None:
            current = self._active_tasks.get(trace_id)
            if current is done:
                self._active_tasks.pop(trace_id, None)

        task.add_done_callback(_cleanup)
        self._attach_background_log(task, trace_id=trace_id)

    def has_active_task(self, trace_id: str) -> bool:
        task = self._active_tasks.get(trace_id)
        if task is None:
            return False
        if task.done():
            self._active_tasks.pop(trace_id, None)
            return False
        return True

    async def cancel_active_task(self, trace_id: str) -> bool:
        task = self._active_tasks.get(trace_id)
        if task is None:
            return False
        if task.done():
            self._active_tasks.pop(trace_id, None)
            return False
        task.cancel()
        await asyncio.sleep(0)
        return True

    def _get_trace_lock(self, trace_id: str) -> asyncio.Lock:
        lock = self._trace_locks.get(trace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._trace_locks[trace_id] = lock
        return lock

    async def _try_enter_queue(self) -> int:
        async with self._queue_counter_lock:
            if self._queued_tasks >= self.global_queue_max:
                raise QueueRejectedError("queue_full", queue_length=self._queued_tasks, wait_ms=0)
            self._queued_tasks += 1
            return self._queued_tasks

    async def _leave_queue(self) -> None:
        async with self._queue_counter_lock:
            self._queued_tasks = max(0, self._queued_tasks - 1)

    async def _admit_and_run(self, trace: Trace, runner, *, request_input: str | None = None) -> QueueAdmission:
        queue_length = await self._try_enter_queue()
        trace_lock = self._get_trace_lock(trace.trace_id)
        queued_at = time.perf_counter()
        start_gate: asyncio.Future = asyncio.get_running_loop().create_future()

        async def _worker() -> None:
            acquired_trace = False
            acquired_slot = False
            try:
                await trace_lock.acquire()
                acquired_trace = True
                await self._run_limiter.acquire()
                acquired_slot = True

                wait_ms = max(0, int((time.perf_counter() - queued_at) * 1000))
                if not start_gate.done():
                    start_gate.set_result(QueueAdmission(queue_length=queue_length, wait_ms=wait_ms))

                if request_input is None:
                    await runner(trace)
                else:
                    await runner(trace, request_input)
            except asyncio.CancelledError:
                if not start_gate.done():
                    start_gate.set_exception(asyncio.CancelledError())
                raise
            except Exception as exc:
                if not start_gate.done():
                    start_gate.set_exception(exc)
                logger.exception("queued run failed: trace=%s err=%s", trace.trace_id, exc)
                raise
            finally:
                if acquired_slot:
                    self._run_limiter.release()
                if acquired_trace:
                    trace_lock.release()
                await self._leave_queue()

        worker_task = asyncio.create_task(_worker())
        self._track_active_task(trace.trace_id, worker_task)
        try:
            admission: QueueAdmission = await asyncio.wait_for(
                start_gate,
                timeout=float(self.queue_wait_timeout_ms) / 1000.0,
            )
            return admission
        except asyncio.TimeoutError as exc:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
            wait_ms = max(0, int((time.perf_counter() - queued_at) * 1000))
            raise QueueRejectedError("queue_timeout", queue_length=queue_length, wait_ms=wait_ms) from exc

    async def dispatch_start(self, trace: Trace) -> QueueAdmission:
        if not self.queue_lanes_enabled:
            task = asyncio.create_task(self.start(trace))
            self._track_active_task(trace.trace_id, task)
            return QueueAdmission(queue_length=0, wait_ms=0)
        return await self._admit_and_run(trace, self.start)

    async def dispatch_hitl(self, trace: Trace, request_input: str) -> QueueAdmission:
        if not self.queue_lanes_enabled:
            task = asyncio.create_task(self.submit_hitl(trace, request_input))
            self._track_active_task(trace.trace_id, task)
            return QueueAdmission(queue_length=0, wait_ms=0)
        return await self._admit_and_run(trace, self.submit_hitl, request_input=request_input)

    async def initialize(self, goal: str, client_id: str) -> Trace:
        trace = Trace(client_id=client_id, node=NodeType.THINK)
        turn = Turn(index=len(trace.turns) + 1, user_input=goal)
        trace.turns.append(turn)
        trace.current_turn_id = turn.turn_id
        self.checkpoint.save(trace)
        return trace

    async def new_turn(self, trace: Trace, goal: str) -> None:
        trace.status = AgentStatus.RUNNING
        trace.node = NodeType.THINK
        trace.error_message = None
        trace.started_at = None
        trace.finished_at = None
        trace.current_step_id = None
        trace.pending_action_id = None
        trace.hitl_ticket = None
        turn = Turn(index=len(trace.turns) + 1, user_input=goal)
        trace.turns.append(turn)
        trace.current_turn_id = turn.turn_id
        self.checkpoint.save(trace)

    async def start(self, trace: Trace) -> None:
        await self.executor.run(trace)

    async def get_trace(self, trace_id: str) -> Optional[Trace]:
        try:
            trace = self.checkpoint.load(trace_id)
        except KeyError:
            return None

        if isinstance(trace.hitl_ticket, dict):
            trace.hitl_ticket = HitlTicket(**trace.hitl_ticket)

        trace.turns = [self._rehydrate_turn(turn) for turn in trace.turns]
        return trace

    def _rehydrate_turn(self, turn: Turn | dict) -> Turn:
        hydrated_turn = turn if isinstance(turn, Turn) else Turn(**turn)
        hydrated_turn.steps = [self._rehydrate_step(step) for step in hydrated_turn.steps]
        return hydrated_turn

    def _rehydrate_step(self, step: Step | dict) -> Step:
        hydrated_step = step if isinstance(step, Step) else Step(**step)
        hydrated_step.actions = [
            action if isinstance(action, Action) else Action(**action)
            for action in (hydrated_step.actions or [])
        ]
        hydrated_step.observations = [
            observation if isinstance(observation, Observation) else Observation(**observation)
            for observation in (hydrated_step.observations or [])
        ]
        return hydrated_step

    async def submit_hitl(self, trace: Trace, request_input: str) -> None:
        await self.executor.execute_hitl(trace, request_input)
        await self.executor.run(trace)
