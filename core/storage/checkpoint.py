from typing import Dict, Any
from dataclasses import asdict
from core.protocol import Trace

class CheckpointStore:
    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def save(self, trace: Trace):
        self._store[trace.trace_id] = asdict(trace)

    def load(self, trace_id: str) -> Trace:
        data = self._store[trace_id]
        return Trace(**data)