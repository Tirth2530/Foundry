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


MANIFEST = config.BENCH / "MANIFEST.json"
SPEC_KEYS = {"id", "title", "request", "tests", "topology", "tags", "note"}


def _norm_tokens(text: str) -> set:
    import re
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {"the", "a", "an", "and", "or", "to", "of", "it", "is",
                                                            "with", "for", "on", "in", "reply", "replies", "agent"}


def fingerprint(spec: dict) -> str:
    """Content fingerprint of a task: normalised request + the hidden test inputs (not the expectations)."""
    sends = [t["send"] for c in spec.get("tests", []) for t in c.get("turns", [])]
    body = json.dumps({"request": " ".join(spec.get("request", "").lower().split()), "sends": sends}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def manifest() -> dict:
    """Frozen manifest: every spec file's SHA-256, its split and content fingerprint, plus split hashes.
    An audit control (changes are detectable), not a secrecy guarantee: the files are plain JSON on disk."""
    files = {}
    for split, d in DIRS.items():
        for pth in sorted(d.glob("*.json")):
            spec = json.loads(pth.read_text())
            files[str(pth.relative_to(MANIFEST.parent))] = {
                "split": split, "id": spec.get("id", pth.stem), "sha256": hashlib.sha256(pth.read_bytes()).hexdigest(),
                "fingerprint": fingerprint(spec), "tests": len(spec.get("tests", [])),
                "multi_agent": bool(spec.get("topology"))}
    return {"schema": "foundry.bench-manifest/1", "files": files,
            "split_hashes": {s: split_hash(s) for s in DIRS}}


def freeze(force_final: bool = False) -> dict:
    """Write MANIFEST.json. Changing the FINAL split after it has been used is refused (that would silently
    change what 'held-out' means); dev and promotion may be extended, and the change is visible in git."""
    new = manifest()
    if MANIFEST.exists() and (config.EVIDENCE / "final.lock").exists() and not force_final:
        old = json.loads(MANIFEST.read_text())
        if old.get("split_hashes", {}).get("final") != new["split_hashes"]["final"]:
            raise PermissionError("the final set changed after it was used; refusing to re-freeze it")
    MANIFEST.write_text(json.dumps(new, indent=1, sort_keys=True) + "\n")
    return new


def integrity() -> dict:
    """Leakage and integrity checks across splits. Reports ids and scores only (never sealed content)."""
    errors, warnings = [], []
    specs = {split: _load_dir(d) for split, d in DIRS.items()}
    ids, fps = {}, {}
    for split, ss in specs.items():
        for sp in ss:
            extra = set(sp) - SPEC_KEYS
            if extra:
                errors.append(f"{split}/{sp['id']}: unknown keys {sorted(extra)}")
            topo = sp.get("topology")
            if topo is not None and not (isinstance(topo, dict) and isinstance(topo.get("agents"), list)
                                         and set(topo.get("must_receive", [])) <= set(topo["agents"])):
                errors.append(f"{split}/{sp['id']}: topology must be {{agents:[...], must_receive:[subset]}}")
            if sp["id"] in ids:
                errors.append(f"duplicate id {sp['id']} in {ids[sp['id']]} and {split}")
            ids[sp["id"]] = split
            f = fingerprint(sp)
            if f in fps:
                errors.append(f"identical task in {fps[f]} and {split}/{sp['id']}")
            fps[f] = f"{split}/{sp['id']}"
    order = list(specs)
    for i, a in enumerate(order):
        for b in order[i + 1:]:
            for x in specs[a]:
                tx = _norm_tokens(x["request"])
                for y in specs[b]:
                    ty = _norm_tokens(y["request"])
                    j = len(tx & ty) / max(1, len(tx | ty))
                    if j >= 0.6:
                        (errors if j >= 0.85 else warnings).append(
                            f"near-duplicate requests {a}/{x['id']} ~ {b}/{y['id']} (jaccard {j:.2f})")
    drift = []
    if MANIFEST.exists():
        frozen = json.loads(MANIFEST.read_text())["files"]
        now = manifest()["files"]
        for k in sorted(set(frozen) | set(now)):
            if frozen.get(k, {}).get("sha256") != now.get(k, {}).get("sha256"):
                drift.append(k)
        if drift:
            errors.append(f"benchmark files differ from MANIFEST.json: {drift}")
    else:
        warnings.append("no MANIFEST.json: run `python -m foundry bench-freeze`")
    prom_uses = len([r for r in ledger.read("promotion_eval") if r.get("split") == "promotion"])
    if prom_uses >= 10:
        warnings.append(f"the promotion set has been evaluated {prom_uses} times: repeated reuse raises the risk of "
                        "overfitting promotion decisions to it; add fresh promotion tasks")
    return {"errors": errors, "warnings": warnings, "promotion_set_evaluations": prom_uses,
            "counts": {s: len(v) for s, v in specs.items()},
            "multi_agent": {s: sum(1 for x in v if x.get("topology")) for s, v in specs.items()}}


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
