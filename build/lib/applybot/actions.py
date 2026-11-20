"""The operations that change state or send mail, in one place.

The command line and the browser dashboard both call these, so the safety
checks (verified drafts only, CV file must exist, daily cap, one send per
application) cannot drift apart between the two front ends.
"""
from __future__ import annotations

import email.utils
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import db, history, mailer, packets


class ActionError(RuntimeError):
    """A refusal with a reason a person can read. Nothing was changed or sent."""


class CapReached(ActionError):
    pass


def load(conn, job_id: str):
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise ActionError(f"no such job: {job_id}")
    score = conn.execute("SELECT * FROM scores WHERE job_id=?", (job_id,)).fetchone()
    app = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
    return job, score, app


# --- reviewing a draft ------------------------------------------------------

def save_letter(conn, job_id: str, subject: str, letter: str, cfg=None) -> None:
    """A human-edited letter. The earlier LLM fact-check issues are cleared and the draft is marked as
    edited by you, but the plain-code checks (numbers, not-claimed technologies, copied posting wording,
    placeholders) run again: an edit can fix a problem, never hide it. Without `cfg` they are skipped."""
    job, _, app = load(conn, job_id)
    if not app:
        raise ActionError("no draft for this job")
    if app["status"] in ("approved", "sent"):
        raise ActionError(f"the application is already {app['status']}; it can no longer be edited")
    letter = letter.strip()
    if not letter:
        raise ActionError("the letter is empty")
    issues: list[str] = []
    if cfg is not None:
        from . import drafting
        issues = drafting.deterministic_issues(letter, job["title"], job["company"], job["description"] or "", cfg)
    db.save_application(conn, job_id, "needs_edit" if issues else "drafted", subject.strip() or app["subject"], letter,
                        {"issues": issues, "edited_by_human": True})
    try:
        history.add(conn, job_id, job["company"], job["title"], "edit", subject.strip() or app["subject"], letter, issues)
    except Exception:
        pass


def approve(cfg, conn, job_id: str) -> Path:
    """Build the outbox packet. A draft with unresolved fact-check problems cannot be approved;
    there is no override. Edit the letter until the flags are gone."""
    job, score, app = load(conn, job_id)
    if not app:
        raise ActionError("no draft for this job")
    if app["status"] == "needs_edit":
        raise ActionError("the draft still has fact-check problems (see the issues). Edit the letter to fix them; "
                          "it cannot be approved while any remain.")
    if app["status"] in ("sent", "rejected"):
        raise ActionError(f"the application is {app['status']}")
    d = packets.build_packet(cfg, job, app, score)
    db.set_application_status(conn, job_id, "approved", str(d))
    return d


def reject(conn, job_id: str) -> None:
    job, _, app = load(conn, job_id)
    if app:
        db.set_application_status(conn, job_id, "rejected")


def mark_applied(conn, job_id: str) -> None:
    """For postings with no email: you applied through the company's form yourself."""
    job, _, app = load(conn, job_id)
    if not app or app["status"] != "approved":
        raise ActionError("only an approved application can be marked as applied")
    db.set_application_status(conn, job_id, "sent")


# --- from "try" into the review queue ------------------------------------------

def trial_job_id(job) -> str:
    key = f"{job.title}|{job.company}|{job.description}".encode("utf-8")
    return "manual:" + hashlib.sha1(key).hexdigest()[:10]


def save_trial(conn, job, verdict, raw: dict, letter: dict | None) -> tuple[str, str]:
    """Put a result from `try` into the review queue exactly as the pipeline would have:
    the job, its score, and the drafted letter (needs_edit if the fact-check failed).
    Saving the same posting twice updates the same entry. If the same company, title and
    location is already stored under another id (an earlier paste, or a posting the pipeline
    fetched), the letter is attached to that existing job instead of failing.
    Returns (application status, id of the job that now holds the letter)."""
    if not letter:
        raise ActionError("this result has no letter to review; run it again without 'score only'")
    if verdict.decision == "reject":
        raise ActionError("rejected jobs are not queued")
    job_id = job.id
    if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
        if not db.upsert_job(conn, job, "scored"):
            same = conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (job.dedupe_key,)).fetchone()
            if not same:
                raise ActionError("could not store this job (same company, title and location, but the stored one cannot be found)")
            job_id = same["id"]
    app = conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()
    if app and app["status"] in ("approved", "sent"):
        raise ActionError(f"this posting is already {app['status']}; it was not overwritten")
    db.save_score(conn, job_id, verdict, raw)
    status = "needs_edit" if letter["issues"] else "drafted"
    db.save_application(conn, job_id, status, letter["subject"], letter["body"],
                        {"issues": letter["issues"], "revisions": letter["revisions"], "from_try": True})
    return status, job_id


# --- sending an application -------------------------------------------------

@dataclass
class Prepared:
    job: object
    score: object
    app: object
    msg: object
    to: str
    cv: Path


def cap_left(cfg, conn) -> int:
    return cfg.settings["mail"]["daily_cap"] - db.count_sent_today(conn)


def cap_message(cfg) -> str:
    return f"daily cap ({cfg.settings['mail']['daily_cap']}) reached; remaining approved applications wait until tomorrow"


def prepare_application(cfg, conn, job_id: str) -> Prepared:
    """Everything that can be checked without touching the network."""
    job, score, app = load(conn, job_id)
    if not app or app["status"] != "approved":
        raise ActionError(f"status is '{app['status'] if app else 'none'}', only approved applications are sent")
    if not score or not score["contact_email"]:
        raise ActionError(f"no email in posting, apply manually via {job['url']}")
    cv = cfg.cv_file(score["cv_id"]) if score["cv_id"] else None
    if cv is None or not cv.exists():
        raise ActionError(f"CV file for '{score['cv_id']}' is missing, refusing to send without it")
    mail = cfg.settings["mail"]
    msg = mailer.build_message(mail["username"], score["contact_email"], app["subject"], app["letter"], cv,
                                attach_as=mail.get("attach_as"))
    return Prepared(job, score, app, msg, score["contact_email"], cv)


def deliver_application(cfg, conn, prep: Prepared) -> str:
    mail = cfg.settings["mail"]
    if cap_left(cfg, conn) <= 0:
        raise CapReached(cap_message(cfg))
    reply = mailer.send(mail, prep.msg)
    db.save_message(conn, message_id=prep.msg["Message-ID"], job_id=prep.job["id"], direction="out",
                    from_addr=mail["username"], to_addr=prep.to,
                    subject=prep.app["subject"], body=prep.app["letter"], status="sent")
    db.set_application_status(conn, prep.job["id"], "sent")
    try:
        history.add(conn, prep.job["id"], prep.job["company"], prep.job["title"], "sent", prep.app["subject"], prep.app["letter"], [])
    except Exception:
        pass
    mailer.save_to_sent(mail, prep.msg)
    return reply


# --- replying to a recruiter -------------------------------------------------

def get_incoming(conn, n: int):
    m = conn.execute("SELECT * FROM messages WHERE id=? AND direction='in'", (n,)).fetchone()
    if not m:
        raise ActionError("no such message")
    return m


def save_reply(conn, n: int, subject: str, text: str) -> None:
    from .replies import _re_subject
    m = get_incoming(conn, n)
    text = text.strip()
    if not text:
        raise ActionError("empty reply, nothing saved")
    subject = subject.strip() or m["draft_subject"] or _re_subject(m["subject"])
    conn.execute("UPDATE messages SET draft_subject=?, draft_body=?, status='draft_ready' WHERE id=?",
                 (subject, text, m["id"]))
    conn.commit()


def prepare_reply(cfg, conn, n: int):
    m = get_incoming(conn, n)
    if m["status"] != "draft_ready":
        raise ActionError(f"status is '{m['status']}': write and save the reply first")
    mail = cfg.settings["mail"]
    to = email.utils.parseaddr(m["from_addr"])[1]
    msg = mailer.build_message(mail["username"], to, m["draft_subject"], m["draft_body"],
                               in_reply_to=m["message_id"], references=f"{m['parent_id']} {m['message_id']}")
    return m, to, msg


def deliver_reply(cfg, conn, m, to: str, msg) -> None:
    mail = cfg.settings["mail"]
    mailer.send(mail, msg)
    db.save_message(conn, message_id=msg["Message-ID"], job_id=m["job_id"], direction="out",
                    parent_id=m["message_id"], from_addr=mail["username"], to_addr=to,
                    subject=m["draft_subject"], body=m["draft_body"], status="sent")
    conn.execute("UPDATE messages SET status='replied' WHERE id=?", (m["id"],))
    conn.commit()
    mailer.save_to_sent(mail, msg)


def save_stages(cfg, mapping: dict[str, str]) -> None:
    """Write llm.stages into config/settings.yaml, leaving every other line and comment alone.

    Only the `stages:` block is replaced (all five stages written out, so nothing is left to
    inherit by accident). The old file is kept as settings.yaml.bak, the new text is parsed
    back and compared before it replaces the file, and the write is atomic."""
    from .llm import PROVIDERS, STAGES
    bad = {k: v for k, v in mapping.items() if k not in STAGES or v not in PROVIDERS}
    missing = [k for k in STAGES if k not in mapping]
    if bad or missing:
        raise ActionError(f"invalid model choice: {bad or ''} {('missing: ' + ', '.join(missing)) if missing else ''}".strip())
    path = cfg.root / "config" / "settings.yaml"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    block = ["  stages:"] + [f"    {k}: {mapping[k]}" for k in STAGES]
    start = next((i for i, l in enumerate(lines) if re.match(r"^  stages:\s*(#.*)?$", l)), None)
    if start is None:
        llm_at = next((i for i, l in enumerate(lines) if re.match(r"^llm:\s*(#.*)?$", l)), None)
        if llm_at is None:
            raise ActionError("settings.yaml has no 'llm:' section to put the stages in")
        end = len(lines)
        for i in range(llm_at + 1, len(lines)):
            if lines[i].strip() and not lines[i].startswith(" "):
                end = i
                break
        while end > llm_at + 1 and not lines[end - 1].strip():
            end -= 1
        new = lines[:end] + block + lines[end:]
    else:
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith("    ")):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        new = lines[:start] + block + lines[end:]
    out = "\n".join(new) + ("\n" if text.endswith("\n") else "")
    try:
        parsed = yaml.safe_load(out)
        ok = parsed["llm"]["stages"] == {k: mapping[k] for k in STAGES}
        same_rest = {k: v for k, v in parsed.items() if k != "llm"} == {k: v for k, v in yaml.safe_load(text).items() if k != "llm"}
    except Exception as e:
        raise ActionError(f"refused to write settings.yaml, the result would not parse: {e}")
    if not (ok and same_rest):
        raise ActionError("refused to write settings.yaml, the result did not match what was chosen")
    path.with_suffix(".yaml.bak").write_text(text, encoding="utf-8")
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)
