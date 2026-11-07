"""Minimal stand-in for uagents, used ONLY by offline tests when the real package is absent."""
import hashlib

from pydantic import BaseModel


class Model(BaseModel):
    pass


class Context:
    pass


class Protocol:
    def __init__(self, spec=None, role=None, name=None, version=None):
        self.spec, self.role, self.handlers = spec, role, {}

    def on_message(self, model, replies=None):
        def deco(fn):
            self.handlers[model] = fn
            return fn
        return deco


class Agent:
    def __init__(self, name="agent", seed="", port=None, mailbox=False, publish_agent_details=False, **kw):
        self.name = name
        self.address = "agent1q" + hashlib.sha256(f"{name}{seed}".encode()).hexdigest()[:50]
        self.events, self.handlers, self.protocols = {}, {}, []

    def on_event(self, kind):
        def deco(fn):
            self.events.setdefault(kind, []).append(fn)
            return fn
        return deco

    def on_message(self, model, replies=None):
        def deco(fn):
            self.handlers[model] = fn
            return fn
        return deco

    def include(self, proto, publish_manifest=False):
        self.protocols.append(proto)
        self.handlers.update(proto.handlers)


class Bureau:
    def __init__(self, *a, **kw):
        self.agents = []

    def add(self, agent):
        self.agents.append(agent)
