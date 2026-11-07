"""Benchmark access with separation of duties.

dev        bench/dev/*.json             builder may learn from full failure details
promotion  bench/sealed/promotion/*.json only the Promotion Judge reads these; it reports
                                          aggregates (pass counts) and never the tests
final      bench/sealed/final/*.json     opened only by `foundry eval-final`; every opening
                                          is counted in the ledger, and its content hash is
                                          recorded so judges can see it never changed
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import config, dsl, ledger

DIRS = {
    "dev": config.BENCH / "dev",
    "promotion": config.BENCH / "sealed" / "promotion",
    "final": config.BENCH / "sealed" / "final",
}


def _load_dir(d: Path) -> list[dict]:
    specs = []
    for p in sorted(d.glob("*.json")):
        spec = json.loads(p.read_text())
        spec.setdefault("id", p.stem)
        specs.append(spec)
    return specs


def split_hash(split: str) -> str:
    h = hashlib.sha256()
    for p in sorted(DIRS[split].glob("*.json")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


_FINAL_UNLOCKED = False  # only evolve.evaluate_final flips this, inside its single-use session


def load(split: str, *, purpose: str) -> list[dict]:
    if split not in DIRS:
        raise ValueError(split)
    if split == "final" and not _FINAL_UNLOCKED:
        raise PermissionError("the final set can only be opened by `foundry eval-final` (single use)")
    if split == "final":
        ledger.append("final_set_opened", purpose=purpose, content_hash=split_hash("final"))
    return _load_dir(DIRS[split])


def public_view(spec: dict) -> dict:
    """What the builder is allowed to see: the request, never the hidden tests."""
    return {"id": spec["id"], "title": spec.get("title", spec["id"]), "request": spec["request"]}


def lint_all() -> dict:
    report = {}
    for split, d in DIRS.items():
        for spec in _load_dir(d):
            errs = []
            if not spec.get("request"):
                errs.append("missing request")
            cases = spec.get("tests", [])
            if len(cases) < 2:
                errs.append("needs >= 2 hidden test cases")
            for c in cases:
                errs += dsl.validate_case(c)
            report[f"{split}/{spec['id']}"] = errs
    return report
