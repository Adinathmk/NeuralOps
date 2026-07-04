"""
fastapi/app/agents/trace_utils.py

Shared LangSmith tracing helpers.
"""
from typing import Any, Dict

def strip_node_state(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Strips non-serializable objects (session, redis) from the state dictionary
    before sending it to LangSmith.
    """
    state = inputs.get("state", {})
    return {
        k: v for k, v in state.items()
        if k not in ("session", "redis")
    }
