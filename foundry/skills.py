"""Versioned skill store: FOUNDRY's own engineering knowledge, as plain files.

skills/versions/v0000/   baseline (what an ordinary coding agent gets)
    config.json          routing (which model family does which role), tools, repair budget
    lessons/*.md         engineering lessons; 000-*.md are locked (never edited by evolution)
skills/CURRENT           the promoted version
skills/candidates/       proposed versions awaiting the promotion gate

Every promotion and rollback is a file copy plus a ledger entry, so any version is
reproducible and diffable.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config

VERSIONS = config.SKILLS / "versions"
CANDIDATES = config.SKILLS / "candidates"
CURRENT = config.SKILLS / "CURRENT"
BASELINE = "v0000"
MAX_LESSON_CHARS = 1800
PATCH_KINDS = {"lesson_add", "lesson_edit", "lesson_remove", "config"}
CONFIG_KEYS = {"routing", "tools", "repair_budget"}
ROLES = {"architect", "builder", "repair", "examiner", "adversary", "miner", "evolver", "foreman"}
TOOLS = {"preflight_static", "run_examiner_tests", "selection_json_probe", "include_reply_text"}


@dataclass
class Skillset:
    version: str
    path: Path
    config: dict
    lessons: list  # [(filename, text)]

    def prompt(self) -> str:
        parts = [f"## {name}\n{text.strip()}" for name, text in self.lessons]
        return "\n\n".join(parts)

    def prompt_tokens(self) -> int:
        return len(self.prompt()) // 4

    def route(self, role: str) -> str:
        return self.config.get("routing", {}).get(role, "gpt")

    def tool(self, name: str) -> bool:
        return bool(self.config.get("tools", {}).get(name, False))


def current_version() -> str:
    return CURRENT.read_text().strip() if CURRENT.exists() else BASELINE


def load(version: str | None = None, path: Path | None = None) -> Skillset:
    version = version or current_version()
    path = path or (VERSIONS / version)
    if not path.exists():
        raise FileNotFoundError(f"skill version {version} not found at {path}")
    cfg = json.loads((path / "config.json").read_text())
    lessons = [(p.name, p.read_text()) for p in sorted((path / "lessons").glob("*.md"))]
    return Skillset(version=version, path=path, config=cfg, lessons=lessons)


def list_versions() -> list[str]:
    return sorted(p.name for p in VERSIONS.iterdir() if p.is_dir()) if VERSIONS.exists() else []


def validate_patch(patch: dict, base: Skillset) -> list[str]:
    errs = []
    kind = patch.get("kind")
    if kind not in PATCH_KINDS:
        return [f"patch kind must be one of {sorted(PATCH_KINDS)}"]
    names = {n for n, _ in base.lessons}
    if kind.startswith("lesson"):
        f = str(patch.get("file", ""))
        if not f.endswith(".md") or "/" in f or ".." in f:
            errs.append("lesson file must be a plain name ending in .md")
        if f.startswith("000-"):
            errs.append("000-* lessons are locked (the hosted contract); evolve around them")
        if kind == "lesson_add" and f in names:
            errs.append(f"{f} exists; use lesson_edit")
        if kind in ("lesson_edit", "lesson_remove") and f not in names:
            errs.append(f"{f} does not exist")
        if kind != "lesson_remove":
            content = str(patch.get("content", ""))
            if not content.strip():
                errs.append("lesson content is empty")
            if len(content) > MAX_LESSON_CHARS:
                errs.append(f"lesson longer than {MAX_LESSON_CHARS} chars; keep lessons small")
    else:
        changes = patch.get("config_changes") or {}
        for k, v in changes.items():
            if k not in CONFIG_KEYS:
                errs.append(f"config key {k!r} not editable")
            elif k == "routing" and (not isinstance(v, dict) or any(r not in ROLES or p not in ("gpt", "meta", "asi") for r, p in v.items())):
                errs.append("routing must map roles to gpt|meta|asi")
            elif k == "tools" and (not isinstance(v, dict) or any(t not in TOOLS for t in v)):
                errs.append(f"tools must be a dict over {sorted(TOOLS)}")
            elif k == "repair_budget" and not (isinstance(v, int) and 0 <= v <= 4):
                errs.append("repair_budget must be an int 0..4")
        if not changes:
            errs.append("config patch has no config_changes")
    return errs


def make_candidate(base: Skillset, patch: dict, cand_id: str) -> Skillset:
    errs = validate_patch(patch, base)
    if errs:
        raise ValueError("; ".join(errs))
    dest = CANDIDATES / cand_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base.path, dest)
    kind = patch["kind"]
    if kind in ("lesson_add", "lesson_edit"):
        (dest / "lessons" / patch["file"]).write_text(patch["content"].strip() + "\n")
    elif kind == "lesson_remove":
        (dest / "lessons" / patch["file"]).unlink()
    else:
        cfg = json.loads((dest / "config.json").read_text())
        for k, v in patch["config_changes"].items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
        (dest / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (dest / "PATCH.json").write_text(json.dumps({"parent": base.version, **patch}, indent=2) + "\n")
    return load(cand_id, dest)


def promote(cand: Skillset) -> str:
    existing = list_versions()
    nxt = f"v{int(existing[-1][1:]) + 1:04d}" if existing else BASELINE
    shutil.copytree(cand.path, VERSIONS / nxt)
    CURRENT.write_text(nxt + "\n")
    return nxt


def rollback(version: str) -> None:
    if not (VERSIONS / version).exists():
        raise FileNotFoundError(version)
    CURRENT.write_text(version + "\n")


def diff_summary(a: Skillset, b: Skillset) -> dict:
    la, lb = dict(a.lessons), dict(b.lessons)
    return {
        "added": sorted(set(lb) - set(la)),
        "removed": sorted(set(la) - set(lb)),
        "edited": sorted(k for k in set(la) & set(lb) if la[k] != lb[k]),
        "config_changed": a.config != b.config,
        "prompt_tokens": [a.prompt_tokens(), b.prompt_tokens()],
    }
