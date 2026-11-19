"""Glue: ingest -> score -> draft. Each stage is idempotent and resumable."""
from __future__ import annotations

import logging

from . import db, history
from .drafting import draft_checked
from .models import Job
from .prefilter import prefilter
from .scoring import score_job

log = logging.getLogger("applybot.pipeline")


def row_to_job(r) -> Job:
    return Job(
        source=r["source"], ext_id=r["id"].split(":", 1)[1], company=r["company"] or "",
        title=r["title"] or "", url=r["url"] or "", location=r["location"] or "",
        remote=None if r["remote"] is None else bool(r["remote"]),
        description=r["description"] or "", employment_type=r["employment_type"] or "",
        posted_at=r["posted_at"] or "",
    )


def ingest(conn, cfg, jobs) -> dict:
    stats = {"seen": 0, "new": 0, "filtered": 0, "duplicate": 0}
    for job in jobs:
        stats["seen"] += 1
        ok, reason = prefilter(job, cfg.criteria)
        inserted = db.upsert_job(conn, job, "new" if ok else "filtered", reason)
        if not inserted:
            stats["duplicate"] += 1
        elif ok:
            stats["new"] += 1
        else:
            stats["filtered"] += 1
    return stats


def score_pending(conn, cfg, llm, limit: int | None = None) -> dict:
    stats = {"scored": 0, "errors": 0, "auto_eligible": 0, "human": 0, "reject": 0}
    q = "SELECT * FROM jobs WHERE status='new' ORDER BY fetched_at"
    rows = conn.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    for r in rows:
        job = row_to_job(r)
        try:
            verdict, raw = score_job(llm, job, cfg)
        except Exception as e:
            log.warning("scoring %s failed: %s", job.id, e)
            stats["errors"] += 1
            continue
        db.save_score(conn, job.id, verdict, raw)
        stats["scored"] += 1
        stats[verdict.decision] += 1
    return stats


def draft_pending(conn, cfg, llm, limit: int | None = None) -> dict:
    stats = {"drafted": 0, "needs_edit": 0, "errors": 0}
    q = """SELECT j.*, s.language AS s_lang FROM jobs j
           JOIN scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE s.decision IN ('human','auto_eligible') AND a.job_id IS NULL
           ORDER BY s.overall DESC"""
    rows = conn.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    for r in rows:
        job = row_to_job(r)
        lang = r["s_lang"] or "en"
        if lang not in {"pt", "en", "es", "fr", "nl"}:
            lang = "en"
        try:
            letter, issues, revisions = draft_checked(llm, job, lang, cfg, record=history.recorder(cfg, job, llm))
        except Exception as e:
            log.warning("drafting %s failed: %s", job.id, e)
            stats["errors"] += 1
            continue
        status = "drafted" if not issues else "needs_edit"
        db.save_application(conn, job.id, status, letter["subject"], letter["body"],
                            {"issues": issues, "revisions": revisions})
        stats[status] += 1
    return stats
