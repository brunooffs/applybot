"""Reply watcher: read-only IMAP poll, match to our sent applications,
classify, and draft SAFE replies into a review queue. Nothing is auto-sent.

Matching is by Message-ID threading (In-Reply-To / References pointing at a
message we sent), so unrelated mail in the inbox is never touched or stored.
Recruiters who write to you first are not matched (not built yet).
"""
from __future__ import annotations

import email
import email.policy
import json
import logging
import re
from datetime import date, timedelta

from . import db, mailer
from .drafting import _facts_for_prompt, verify_letter
from .llm import for_stage
from .models import Job
from .text import html_to_text

log = logging.getLogger("applybot.replies")

# Anything touching money, dates you must commit to, or legal status is yours
# to answer. Deliberately over-broad: a false alarm costs you a minute.
ESCALATE = re.compile(
    r"\b(salary|salário|salario|day rate|hourly rate|rate|compensation|expectations?|package|"
    r"notice period|aviso prévio|start date|available from|availability|disponibilidade|"
    r"visa|work permit|authori[sz]ation|clearance|relocat\w*|nda|contract terms)\b",
    re.I,
)
DRAFTABLE = {"interview_request", "screening_questions", "info_request"}
_BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster", re.I)
_BOUNCE_SUBJ = re.compile(r"delivery (status notification|failed|has failed)|undeliver|returned mail|failure notice", re.I)
_SMTP_ERR = re.compile(r"[45]\d\d[ -]\d\.\d{1,3}\.\d{1,3}[^\n]*")


def looks_like_bounce(from_addr: str, subject: str) -> bool:
    return bool(_BOUNCE_FROM.search(from_addr or "") or _BOUNCE_SUBJ.search(subject or ""))
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MSGID = re.compile(r"<[^>]+>")

CLASSIFY_SYSTEM = """You triage replies to job applications. Output ONLY JSON:
{"category": "interview_request"|"screening_questions"|"info_request"|"rejection"|"auto_reply"|"other",
 "summary": "one sentence",
 "needs_candidate_decision": true|false}
needs_candidate_decision is true if the sender asks for a commitment, a decision, or personal details only the candidate can give."""

REPLY_SYSTEM = """You write a reply email for the candidate, answering a recruiter's message about an application.
Output ONLY JSON: {"subject": str, "body": str}
Rules:
- Use ONLY FACTS. Never invent anything. Answer only what was asked, briefly (under 150 words).
- Never state salary, rates, notice periods, availability dates, visa or authorisation status, or any commitment. If the message requires one, output {"subject": "", "body": ""}.
- If they want to schedule a call, say you are glad to talk and ask them to propose times.
- Reply in the language of the incoming message if it is Portuguese, English, Spanish, French or Dutch, otherwise English.
- Sign off with the candidate's name."""


def parse_incoming(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        text = html_to_text(text)
    refs = _MSGID.findall(str(msg.get("In-Reply-To", ""))) + _MSGID.findall(str(msg.get("References", "")))
    mid = _MSGID.findall(str(msg.get("Message-ID", "")))
    return {
        "message_id": mid[0] if mid else "",
        "refs": refs,
        "from_addr": str(msg.get("From", "")),
        "to_addr": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "")),
        "body": text.strip(),
    }


def _imap_since(days: int) -> str:
    d = date.today() - timedelta(days=days)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def poll(conn, cfg) -> int:
    """Fetch recent inbox mail (read-only, does not mark anything read) and
    store those that answer something we sent. Returns number stored."""
    mail = cfg.settings["mail"]
    imap = mailer.imap_connect(mail)
    stored = 0
    try:
        imap.select(mail.get("inbox_folder", "INBOX"), readonly=True)
        typ, data = imap.search(None, "SINCE", _imap_since(mail.get("reply_lookback_days", 14)))
        for num in (data[0] or b"").split():
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                stored += store_if_reply(conn, parse_incoming(raw))
    finally:
        imap.logout()
    return stored


def store_if_reply(conn, inc: dict) -> int:
    if not inc["message_id"] or db.message_exists(conn, inc["message_id"]):
        return 0
    parent = db.find_outbound(conn, inc["refs"])
    if parent is None:
        return 0
    rid = db.save_message(
        conn, message_id=inc["message_id"], job_id=parent["job_id"], direction="in",
        parent_id=parent["message_id"], from_addr=inc["from_addr"], to_addr=inc["to_addr"],
        subject=inc["subject"], body=inc["body"], status="new",
    )
    return 1 if rid else 0


def process(conn, cfg, llm) -> dict:
    stats = {"draft_ready": 0, "needs_edit": 0, "needs_you": 0, "no_action": 0, "bounce": 0, "errors": 0}
    for r in conn.execute("SELECT * FROM messages WHERE direction='in' AND status='new'").fetchall():
        try:
            status, f = _process_one(conn, cfg, llm, r)
        except Exception as e:
            log.warning("processing message %s failed: %s", r["id"], e)
            stats["errors"] += 1
            continue
        conn.execute(
            """UPDATE messages SET category=?, summary=?, status=?, reasons=?, draft_subject=?, draft_body=?
               WHERE id=?""",
            (f["category"], f["summary"], status, json.dumps(f["reasons"]), f["subject"], f["body"], r["id"]),
        )
        conn.commit()
        stats[status] += 1
    return stats


def _process_one(conn, cfg, llm, r):
    if looks_like_bounce(r["from_addr"], r["subject"]):
        m = _SMTP_ERR.search(r["body"] or "")
        reason = m.group(0).strip() if m else (r["body"] or "")[:200].strip()
        db.set_application_status(conn, r["job_id"], "bounced")
        return "bounce", {"category": "bounce", "summary": f"delivery failed: {reason}",
                          "reasons": ["the message did not reach the recipient"], "subject": "", "body": ""}
    c = for_stage(llm, "reply").complete_json(CLASSIFY_SYSTEM, f"Subject: {r['subject']}\n\n{r['body'][:6000]}")
    f = {"category": c.get("category", "other"), "summary": c.get("summary", ""),
         "reasons": [], "subject": "", "body": ""}
    if f["category"] not in DRAFTABLE:
        return "no_action", f

    hits = sorted({m.group(0).lower() for m in ESCALATE.finditer(f"{r['subject']}\n{r['body']}")})
    if hits:
        f["reasons"].append("mentions " + ", ".join(hits) + ": you answer this one")
    if c.get("needs_candidate_decision"):
        f["reasons"].append("asks for a decision or details only you can give")
    if f["reasons"]:
        return "needs_you", f

    app = conn.execute("SELECT subject, letter FROM applications WHERE job_id=?", (r["job_id"],)).fetchone()
    user = (f"FACTS:\n{_facts_for_prompt(cfg)}\n\nYOUR ORIGINAL APPLICATION:\n{app['letter'] if app else ''}\n\n"
            f"INCOMING MESSAGE (subject: {r['subject']}):\n{r['body'][:6000]}")
    d = for_stage(llm, "reply").complete_json(REPLY_SYSTEM, user)
    body = str(d.get("body", "")).strip()
    if not body:
        f["reasons"].append("model declined to draft (needs a commitment or personal detail)")
        return "needs_you", f
    subject = str(d.get("subject", "")).strip() or _re_subject(r["subject"])
    f["subject"], f["body"] = subject, body
    job = Job(source="mail", ext_id=str(r["id"]), company="", title=r["subject"] or "", url="", description=r["body"])
    issues = verify_letter(llm, {"subject": subject, "body": body}, job, cfg)
    if issues:
        f["reasons"] = issues
        return "needs_edit", f
    return "draft_ready", f


def _re_subject(s: str) -> str:
    s = s or ""
    return s if s.lower().startswith("re:") else f"Re: {s}"
