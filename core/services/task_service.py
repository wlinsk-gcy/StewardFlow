from core.tools.tool import ToolRegistry
from core.llm import Provider
from core.storage.checkpoint import CheckpointStore
from ws.connection_manager import ConnectionManager
from core.executor import TaskExecutor
from core.cache_manager import CacheManager
from core.tool_result_externalizer import ToolResultExternalizerConfig

from core.protocol import Trace,Turn,Step,NodeType,Action,Observation,AgentStatus

class TaskService:

    def __init__(self, checkpoint: CheckpointStore, provider: Provider, tool_registry: ToolRegistry,
                 ws_manager: ConnectionManager, cache_manager: CacheManager,
                 tool_result_config: ToolResultExternalizerConfig | None = None):
        self.checkpoint = checkpoint
        self.provider = provider
        self.tool_registry = tool_registry
        self.ws_manager = ws_manager
        self.cache_manager = cache_manager
        self.executor = TaskExecutor(
            checkpoint,
            provider,
            tool_registry,
            ws_manager,
            cache_manager,
            tool_result_config=tool_result_config,
        )

    async def initialize(self, goal: str, client_id: str) -> Trace:
        trace = Trace(client_id=client_id,node=NodeType.THINK)
        turn = Turn(index=len(trace.turns) + 1, user_input=goal)
        trace.turns.append(turn)
        trace.current_turn_id = turn.turn_id
        self.checkpoint.save(trace)
        return trace

    async def new_turn(self, trace: Trace, goal: str):
        trace.status = AgentStatus.RUNNING
        trace.node = NodeType.THINK
        turn = Turn(index=len(trace.turns) + 1, user_input=goal)
        trace.turns.append(turn)
        trace.current_turn_id = turn.turn_id
        self.checkpoint.save(trace)


    async def start(self, trace: Trace):
        try:
            await self.executor.run(trace)
        except Exception as e:
            raise e

    async def get_trace(self, trace_id: str) -> Trace:
        trace = self.checkpoint.load(trace_id)
        turns = [Turn(**turn) for turn in trace.turns]
        for turn in turns:
            steps = [Step(**step) for step in turn.steps]
            for step in steps:
                actions = [Action(**action) for action in step.actions]
                step.actions = actions
                if step.observations:
                    observations = [Observation(**observation) for observation in step.observations]
                    step.observations = observations
            turn.steps = steps
        trace.turns = turns
        return trace


    async def submit_hitl(self, trace: Trace, request_input: str):
        await self.executor.execute_hitl(trace, request_input)
        await self.executor.run(trace)









