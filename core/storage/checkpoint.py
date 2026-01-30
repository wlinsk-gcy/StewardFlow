from typing import Dict, Any
from dataclasses import asdict
from core.protocol import AgentState,Trace,Turn,Step,ActionV2,ObservationV2

class CheckpointStore:
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def save(self, state: AgentState):
        self._store[state.agent_id] = asdict(state)

    def load(self, agent_id: str) -> AgentState:
        data = self._store[agent_id]
        return AgentState(**data)

    def save_v2(self, trace: Trace):
        self._store[trace.trace_id] = asdict(trace)

    def load_v2(self, trace_id: str) -> Trace:
        data = self._store[trace_id]
        return Trace(**data)