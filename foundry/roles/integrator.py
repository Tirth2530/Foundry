"""Integrator: wires a generated system together and enforces the hosted contract before
anything runs (imports, no Agent()/run(), protocol registered, peers resolvable)."""
from __future__ import annotations

import re

from .. import static_check


def check(build: dict) -> dict:
    names = {a["name"] for a in build["agents"]}
    out = {}
    for a in build["agents"]:
        errs = static_check.check(a["code"], entry=a["name"] == build.get("entry"))
        for ref in re.findall(r"PEERS\[\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]", a["code"]):
            if ref not in names:
                errs.append(f"PEERS['{ref}'] referenced but no agent named '{ref}' exists (agents: {sorted(names)})")
        if errs:
            out[a["name"]] = errs
    if build.get("entry") not in names:
        out["_system"] = [f"entry agent {build.get('entry')!r} is not one of {sorted(names)}"]
    return out
