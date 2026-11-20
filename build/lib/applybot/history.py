"""Every letter the bot writes or you change, kept as a history (table letter_versions).

Recording is best effort: a problem here must never stop a draft, an edit or a send."""
from __future__ import annotations

import difflib
import html
import json
import logging

from . import db

log = logging.getLogger("applybot.history")
KINDS = ("draft", "revision", "edit", "sent")


def add(conn, job_id: str, company: str, title: str, kind: str, subject: str, body: str,
        issues: list | None = None, models: str = "") -> None:
    conn.execute(
        "INSERT INTO letter_versions (job_id, company, title, at, kind, models, subject, body, issues) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, company or "", title or "", db.now(), kind, models, subject or "", body or "", json.dumps(list(issues or []))))
    conn.commit()


def models_of(llm) -> str:
    try:
        return llm.describe()
    except Exception:
        return ""


def recorder(cfg, job, llm):
    """A callable for draft_checked: record(kind, letter, issues). Opens its own short connection."""
    models = models_of(llm)

    def record(kind: str, letter: dict, issues: list[str]) -> None:
        try:
            conn = db.connect(cfg.path("db"))
            try:
                add(conn, job.id, job.company, job.title, kind, letter.get("subject", ""), letter.get("body", ""), issues, models)
            finally:
                conn.close()
        except Exception as e:
            log.warning("could not record letter history: %s", e)
    return record


def postings(conn):
    return conn.execute(
        """SELECT job_id, MAX(company) company, MAX(title) title, COUNT(*) n, MAX(at) last_at,
                  (SELECT status FROM applications a WHERE a.job_id = v.job_id) status
           FROM letter_versions v GROUP BY job_id ORDER BY MAX(id) DESC""").fetchall()


def versions(conn, job_id: str):
    return conn.execute("SELECT * FROM letter_versions WHERE job_id=? ORDER BY id", (job_id,)).fetchall()


def word_diff_html(old: str, new: str) -> str:
    """Escaped HTML with <del>/<ins> around changed words; safe to embed as is."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append("<del>" + html.escape(" ".join(a[i1:i2])) + "</del>")
            if j2 > j1:
                out.append("<ins>" + html.escape(" ".join(b[j1:j2])) + "</ins>")
    return " ".join(out)
