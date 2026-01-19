from typing import Dict, Any
from dataclasses import asdict
from core.protocol import AgentState

class CheckpointStore:
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def save(self, state: AgentState):
        self._store[state.agent_id] = asdict(state)

    def load(self, agent_id: str) -> AgentState:
        data = self._store[agent_id]
        return AgentState(**data)