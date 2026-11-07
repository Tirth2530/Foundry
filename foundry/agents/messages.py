"""Typed messages between FOUNDRY's agents."""
from uagents import Model


class RoleTask(Model):
    rid: str
    role: str
    payload: str  # JSON: {"op", "kwargs", "skills_path"?, "skills_version"?, "cap_remaining"?}


class RoleResult(Model):
    rid: str
    role: str
    ok: bool
    payload: str  # JSON: {"result", "calls", "events", "error"?, "budget"?, "ms"}
