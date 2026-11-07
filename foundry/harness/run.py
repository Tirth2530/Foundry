"""Parent side of the Execution Runner: launch the isolated child, parse, judge."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import config, dsl


def run(agents: list[dict], entry: str, cases: list[dict], *, turn_timeout: float | None = None) -> dict:
    """Execute cases against the generated agents. Returns {raw, results, stderr_tail}."""
    turn_timeout = turn_timeout or config.TURN_TIMEOUT_S
    n_turns = max(1, sum(len(c["turns"]) for c in cases))
    hard = 25 + turn_timeout * n_turns
    job = {"agents": agents, "entry": entry, "turn_timeout": turn_timeout, "hard_limit": hard,
           "cases": [{"name": c.get("name"), "turns": [{"send": t["send"]} for t in c["turns"]]} for c in cases]}
    # The job file must be readable by the sandbox's non-root user (uid 10001), so it goes in a
    # dedicated dir with 0755/0644 instead of a 0600 NamedTemporaryFile. It contains no secrets
    # and no test expectations: only the agent code and the messages to send.
    job_dir = tempfile.mkdtemp(prefix="foundry_job_")
    os.chmod(job_dir, 0o755)
    path = os.path.join(job_dir, "job.json")
    with open(path, "w") as f:
        json.dump(job, f)
    os.chmod(path, 0o644)
    mode = sandbox_mode()
    work = tempfile.mkdtemp(prefix="foundry_run_")  # agent storage files land here, not in the repo
    try:
        p = subprocess.run(_command(mode, path, work, hard), cwd=work, capture_output=True, text=True,
                           timeout=hard + 30, env=_clean_env(work),
                           preexec_fn=_limits(hard) if mode == "process" and os.name == "posix" else None)
        out, err = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")) + "\n[parent] child killed: timeout"
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    raw = None
    for line in out.splitlines():
        if line.startswith("FOUNDRY_RESULT:"):
            raw = json.loads(line[len("FOUNDRY_RESULT:"):])
    stderr_tail = _interesting(err)
    if raw is None:
        raw = {"load_errors": {"__harness__": "runner produced no result"}, "cases": []}
    return {"raw": raw, "results": judge(cases, raw), "stderr_tail": stderr_tail, "sandbox": mode}


# ---------------------------------------------------------------- sandbox
# docker  : no network, read-only root, tmpfs work dir, 768MB RAM, 1 CPU, 256 pids, all caps dropped,
#           non-root user, nothing mounted except the runner and the job file (read-only).
# process : fallback when Docker isn't available: separate process in isolated mode (-I: no user site,
#           no PYTHONPATH, FOUNDRY not importable), EMPTY environment (no API keys), temp working dir,
#           CPU/file-size/open-file limits. Weaker: it can still read files your user can read, which is
#           why the Integrator also statically forbids files, env, eval/exec, os/sys/subprocess.
RUNNER = Path(__file__).resolve().parent / "runner.py"
IMAGE = config.env("FOUNDRY_SANDBOX_IMAGE", "foundry-sandbox:1")


class InfrastructureError(RuntimeError):
    """The environment is broken (Docker down, image missing). Never a lesson for evolution."""


def docker_status() -> str:
    if not shutil.which("docker"):
        return "no-docker"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return "daemon-down"
        if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20).returncode != 0:
            return "no-image"
    except subprocess.TimeoutExpired:
        return "daemon-down"
    return "ok"


def sandbox_mode() -> str:
    want = config.env("FOUNDRY_SANDBOX", "auto")
    if want == "process":
        return "process"
    st = docker_status()
    if st == "ok":
        return "docker"
    if want == "docker":
        msg = {"no-docker": "Docker is not installed",
               "daemon-down": "Docker Desktop is not running: open it (`open -a Docker`) and wait for 'Engine running'",
               "no-image": f"sandbox image {IMAGE} is missing: run `python -m foundry sandbox-build`"}[st]
        raise InfrastructureError(f"SANDBOX UNAVAILABLE: {msg}")
    return "process"


def _command(mode: str, job_path: str, work: str, hard: float) -> list[str]:
    if mode == "docker":
        return ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=64m",
                "--tmpfs", "/work:rw,size=64m,uid=10001", "-w", "/work", "-e", "HOME=/work", "--memory", "768m",
                "--cpus", "1", "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", f"{RUNNER}:/sandbox/runner.py:ro", "-v", f"{job_path}:/sandbox/job.json:ro",
                IMAGE, "python", "-I", "/sandbox/runner.py", "/sandbox/job.json"]
    return [sys.executable, "-I", str(RUNNER), job_path]


def _clean_env(work: str) -> dict:
    """No API keys, no tokens, nothing from your shell except what Python needs to start."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": work, "TMPDIR": work, "LANG": "C.UTF-8"}
    for k in ("SYSTEMROOT", "DOCKER_HOST"):  # Windows python / remote docker need these
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _limits(hard: float):
    def apply():
        import resource
        for lim, val in ((resource.RLIMIT_CPU, int(hard) + 30), (resource.RLIMIT_FSIZE, 64 * 2**20),
                         (resource.RLIMIT_NOFILE, 1024), (getattr(resource, "RLIMIT_AS", None), 3 * 2**30)):
            if lim is None:
                continue
            try:
                resource.setrlimit(lim, (val, val))
            except (ValueError, OSError):
                pass  # e.g. macOS does not support RLIMIT_AS
        os.setsid()
    return apply


def judge(cases: list[dict], raw: dict) -> list[dict]:
    load_errors = raw.get("load_errors") or {}
    results = []
    raw_cases = raw.get("cases") or []
    for i, case in enumerate(cases):
        if load_errors.get(raw.get("entry")) or "__harness__" in load_errors:
            results.append({"name": case.get("name"), "passed": False, "turns": [{
                "send": case["turns"][0]["send"], "ok": False, "reply_text": "", "cards": 0,
                "checks": [{"type": "load", "ok": False, "code": dsl.LOAD,
                            "detail": next(iter(load_errors.values()))[-600:]}]}]})
            continue
        rec = (raw_cases[i] if i < len(raw_cases) else None) or {"turns": [{"error": "case not executed"}]}
        results.append(dsl.evaluate_case(case, rec))
    return results


def _interesting(err: str, limit: int = 2500) -> str:
    keep = [l for l in err.splitlines()
            if any(k in l for k in ("Error", "Traceback", "Exception", "File \"<agent", "raise", "WARNING", "ERROR"))
            and "Almanac" not in l and "registration" not in l.lower() and "endpoint" not in l.lower()]
    return "\n".join(keep)[-limit:]


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(1 for r in results if r["passed"])
    return {"cases": n, "passed": passed, "rate": round(passed / n, 3) if n else 0.0}
