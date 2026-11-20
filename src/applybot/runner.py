"""Sources and runs for the dashboard: edit sources.yaml, test a board, run ingest/score/draft in the background.

One run at a time. Progress is kept in memory (a restart forgets it, the database keeps the results).
Each run opens its own database connection, so the web server stays responsive while a slow local
model works through the queue.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import httpx
import yaml

from . import db, pipeline
from .config import load_config

TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
KINDS = ("greenhouse", "lever", "ashby")
# Remotive's category slugs. Only software-dev and devops are confirmed live here; check any other
# with "Check" on the Sources page before relying on it.
REMOTIVE_KNOWN = ["software-dev", "devops", "data", "product", "qa", "design", "customer-support", "all-others"]

CHECK_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "remotive": "https://remotive.com/api/remote-jobs?category={t}&limit=1000",
}


class SourceError(Exception):
    pass


def _count(kind: str, data) -> int:
    if kind == "lever":
        return len(data) if isinstance(data, list) else 0
    if isinstance(data, dict):
        return len(data.get("jobs") or [])
    return 0


def check_source(kind: str, name: str, http=None) -> tuple[bool, str]:
    """Does this board token / category exist, and how many postings does it list right now?"""
    name = (name or "").strip()
    if kind not in CHECK_URLS:
        return False, f"unknown source type '{kind}'"
    if not TOKEN_RE.match(name):
        return False, f"'{name}' is not a valid token (letters, digits, dot, dash, underscore)"
    own = http is None
    http = http or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "applybot/0.1 (token check)"})
    try:
        r = http.get(CHECK_URLS[kind].format(t=name))
        if r.status_code == 404:
            return False, f"{kind}/{name}: not found (404). Check the spelling against the company's careers URL."
        if r.status_code >= 400:
            return False, f"{kind}/{name}: HTTP {r.status_code}"
        n = _count(kind, r.json())
        if kind == "remotive" and n == 0:
            return False, f"remotive/{name}: answered, but lists 0 jobs. The category slug is probably wrong."
        return True, f"{kind}/{name}: OK, {n} posting{'s' if n != 1 else ''} listed"
    except Exception as e:
        return False, f"{kind}/{name}: {type(e).__name__}: {e}"
    finally:
        if own:
            http.close()


def read_sources(cfg) -> dict:
    s = cfg.sources or {}
    rem = s.get("remotive") or {}
    return {"greenhouse": list(s.get("greenhouse") or []), "lever": list(s.get("lever") or []),
            "ashby": list(s.get("ashby") or []),
            "remotive": {"enabled": bool(rem.get("enabled")), "categories": list(rem.get("categories") or [])}}


def parse_tokens(text: str) -> list[str]:
    """Free text (one per line or comma separated) -> validated, de-duplicated tokens."""
    out: list[str] = []
    for t in re.split(r"[\s,;]+", text or ""):
        t = t.strip()
        if not t:
            continue
        if not TOKEN_RE.match(t):
            raise SourceError(f"'{t}' is not a valid board token (letters, digits, dot, dash, underscore only)")
        if t not in out:
            out.append(t)
    return out


HEADER = """# Where jobs come from. Only sources that publish machine-readable boards.
# LinkedIn and Indeed are deliberately NOT here (ToS / account-ban risk).
# Edited by the dashboard's Sources page; the previous file is kept as sources.yaml.bak.
#   boards.greenhouse.io/<token>   jobs.lever.co/<token>   jobs.ashbyhq.com/<token>
"""


def save_sources(cfg, sources: dict) -> None:
    clean = {k: parse_tokens(" ".join(sources.get(k) or [])) for k in KINDS}
    rem = sources.get("remotive") or {}
    cats = parse_tokens(" ".join(rem.get("categories") or []))
    clean["remotive"] = {"enabled": bool(rem.get("enabled")), "categories": cats}
    path = cfg.root / "config" / "sources.yaml"
    out = HEADER + "\n" + yaml.safe_dump(clean, sort_keys=False, default_flow_style=None, allow_unicode=True)
    if yaml.safe_load(out) != clean:
        raise SourceError("could not write sources.yaml safely")
    if path.exists():
        (path.with_name(path.name + ".bak")).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)


def anthropic_key_problem(cfg, steps) -> str | None:
    """A stage that will use Anthropic with no key in the environment, for the steps being run."""
    from .llm import STAGES
    llm = cfg.settings.get("llm", {})
    default, stages = llm.get("provider", "ollama"), llm.get("stages") or {}
    needs = {"score": "score" in steps, "draft": "draft" in steps, "verify": "draft" in steps, "revise": "draft" in steps}
    bad = [s for s in STAGES if needs.get(s) and stages.get(s, default) == "anthropic"]
    if bad and not os.environ.get("ANTHROPIC_API_KEY"):
        return ("Stage(s) " + ", ".join(bad) + " use Anthropic but ANTHROPIC_API_KEY is not set in the environment this "
                "server was started from. Export it and restart, or switch those stages to ollama under Models.")
    return None


# --- a background run ---------------------------------------------------------------

class Run:
    """State of the current (or last) run. Mutated by the worker thread, read by the page."""

    def __init__(self, steps, options):
        self.steps, self.options = list(steps), dict(options)
        self.state = "running"            # running | done | stopped | failed
        self.stage = "starting"
        self.done = self.total = 0
        self.current = ""
        self.log: list[str] = []
        self.results: dict[str, dict] = {}
        self.stop = threading.Event()
        self.started = time.time()
        self.finished: float | None = None

    def say(self, msg: str) -> None:
        self.log.append(msg)
        del self.log[:-200]


_LOCK = threading.Lock()
CURRENT: Run | None = None


def is_running() -> bool:
    return CURRENT is not None and CURRENT.state == "running"


def start(home, make_llm, steps, options) -> Run:
    """Start a run in a thread. Raises SourceError if one is already running or the options are unusable."""
    global CURRENT
    with _LOCK:
        if is_running():
            raise SourceError("a run is already in progress")
        run = Run(steps, options)
        CURRENT = run
    threading.Thread(target=_work, args=(run, home, make_llm), daemon=True).start()
    return run


def _work(run: Run, home: Path, make_llm) -> None:
    conn = None
    try:
        cfg = load_config(home)
        conn = db.connect(cfg.path("db"))
        llm = make_llm(cfg) if ({"score", "draft"} & set(run.steps)) else None
        o = run.options

        def prog(stage):
            def f(i, total, job):
                run.stage, run.done, run.total = stage, i, total
                run.current = f"{job.company}: {job.title}"
            return f

        if "ingest" in run.steps:
            run.stage, run.current = "ingest", "fetching boards"
            run.say("Fetching job boards…")
            from .sources import iter_jobs
            errs: list[str] = []

            def on_source(kind, name):
                run.current = f"{kind}/{name}"

            def on_error(kind, name, e):
                errs.append(f"{kind}/{name}: {e}")
                run.say(f"  ! {kind}/{name} failed: {e}")

            def stoppable():
                for j in iter_jobs(cfg, on_error=on_error, on_source=on_source):
                    if run.stop.is_set():
                        return
                    yield j
            st = pipeline.ingest(conn, cfg, stoppable())
            st["board_errors"] = len(errs)
            run.results["ingest"] = st
            run.say("Ingest: " + ", ".join(f"{k} {v}" for k, v in st.items()))
        def on_job_error(stage):
            def f(job, e):
                run.say(f"  ! {stage} failed for {job.company}: {job.title}: {type(e).__name__}: {e}")
            return f

        if "score" in run.steps and not run.stop.is_set():
            run.say(f"Scoring up to {o['score_limit']} jobs…")
            st = pipeline.score_pending(conn, cfg, llm, limit=o["score_limit"], progress=prog("score"),
                                        should_stop=run.stop.is_set, on_error=on_job_error("score"))
            run.results["score"] = st
            run.say("Score: " + ", ".join(f"{k} {v}" for k, v in st.items()))
        if "draft" in run.steps and not run.stop.is_set():
            run.say(f"Drafting up to {o['draft_limit']} letters (minimum score {o['min_score']})…")
            st = pipeline.draft_pending(conn, cfg, llm, limit=o["draft_limit"], min_score=o["min_score"],
                                        progress=prog("draft"), should_stop=run.stop.is_set,
                                        on_error=on_job_error("draft"))
            run.results["draft"] = st
            run.say("Draft: " + ", ".join(f"{k} {v}" for k, v in st.items()))
        run.state = "stopped" if run.stop.is_set() else "done"
        run.say("Stopped." if run.state == "stopped" else "Finished.")
    except Exception as e:
        run.state = "failed"
        run.say(f"Failed: {type(e).__name__}: {e}")
    finally:
        run.stage, run.current = "idle", ""
        run.finished = time.time()
        if conn is not None:
            conn.close()
