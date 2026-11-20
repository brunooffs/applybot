"""Approved applications become a self-contained folder in outbox/."""
from __future__ import annotations

import re
import shutil
from pathlib import Path


def safe_name(job_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", job_id)


def build_packet(cfg, job, app, score) -> Path:
    d = cfg.path("outbox") / safe_name(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "letter.md").write_text(f"# {app['subject']}\n\n{app['letter']}\n", encoding="utf-8")
    (d / "job.md").write_text(
        f"# {job['title']} at {job['company']}\n\n"
        f"- URL: {job['url']}\n- Location: {job['location']}\n"
        f"- Family: {score['family']} | CV: {score['cv_id']} | Score: {score['overall']}\n"
        f"- Contact email: {score['contact_email'] or 'none in posting, apply via the URL'}\n",
        encoding="utf-8",
    )
    cv = cfg.cv_file(score["cv_id"]) if score["cv_id"] else None
    if cv and cv.exists():
        shutil.copy2(cv, d / cv.name)
    return d
