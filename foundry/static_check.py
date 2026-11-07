"""Integrator's static checks: does generated code satisfy the Agentverse hosted contract?

Hosted agents get a pre-created global `agent`; only the standard library plus
uagents / uagents_core can be imported; they must not create Agent()/Bureau or call run().
"""
from __future__ import annotations

import ast
import sys

ALLOWED_THIRD_PARTY = {"uagents", "uagents_core", "pydantic"}
FORBIDDEN_MODULES = {"subprocess", "multiprocessing", "ctypes", "socket", "importlib", "shutil", "glob",
                     "pathlib", "pickle", "marshal", "inspect", "gc", "sys", "builtins", "threading", "signal",
                     "os", "io", "tempfile", "fileinput", "zipfile", "tarfile", "sqlite3", "pty", "resource"}
# Hosted agents keep state in ctx.storage; they never need the filesystem, environment or dynamic code.
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "globals", "vars", "breakpoint", "input"}
FORBIDDEN_ATTRS = {"environ", "getenv", "putenv", "system", "popen", "spawnl", "execv", "listdir", "scandir",
                   "walk", "remove", "unlink", "rmdir", "chdir", "fork", "kill", "__subclasses__", "__globals__",
                   "__builtins__", "__code__"}
STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def check(code: str, entry: bool = True) -> list[str]:
    errs: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError line {e.lineno}: {e.msg}"]
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for n in names:
                top = n.split(".")[0]
                if top in FORBIDDEN_MODULES:
                    errs.append(f"import of '{top}' is not allowed in hosted agents")
                elif top and top not in STDLIB and top not in ALLOWED_THIRD_PARTY:
                    errs.append(f"import '{top}' is unavailable on Agentverse hosted agents "
                                f"(only stdlib + uagents/uagents_core)")
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
                errs.append(f"call to {fn.id}() is not allowed: hosted agents must not touch files, the "
                            "environment or dynamic code (use ctx.storage for state)")
            if isinstance(fn, ast.Name) and fn.id in ("getattr", "setattr", "delattr") and not (
                    len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                    and not node.args[1].value.startswith("_")):
                errs.append(f"{fn.id}() with a computed or private attribute name is not allowed")
            if name in ("Agent", "Bureau"):
                errs.append(f"do not construct {name}(): the hosted platform provides the global `agent`")
            if isinstance(fn, ast.Attribute) and fn.attr == "run" and isinstance(fn.value, ast.Name) \
                    and fn.value.id in ("agent", "bureau"):
                errs.append("do not call agent.run(); the platform runs the agent")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            errs.append(f"use of .{node.attr} is not allowed in hosted agents (no environment, files or processes)")
        if isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__", "__spec__"):
            errs.append(f"access to {node.id} is not allowed")
    if "agent.include(" not in code and "@agent.on_message" not in code:
        errs.append("protocol never registered: call agent.include(protocol, publish_manifest=True)")
    if entry and "ChatAcknowledgement" not in code:
        errs.append("no ChatAcknowledgement handling: ACP requires acknowledging every ChatMessage")
    return errs
