from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import actions, db, mailer, pipeline, replies, trial
from .config import load_config
from .llm import LLM
from .models import Job
from .sources import iter_jobs


def _ctx(args):
    cfg = load_config(args.home)
    conn = db.connect(cfg.path("db"))
    return cfg, conn


def cmd_ingest(args):
    cfg, conn = _ctx(args)
    print(pipeline.ingest(conn, cfg, iter_jobs(cfg)))


def cmd_score(args):
    cfg, conn = _ctx(args)
    print(pipeline.score_pending(conn, cfg, LLM(cfg.settings), args.limit))


def cmd_draft(args):
    cfg, conn = _ctx(args)
    print(pipeline.draft_pending(conn, cfg, LLM(cfg.settings), args.limit))


def cmd_run(args):
    cfg, conn = _ctx(args)
    llm = LLM(cfg.settings)
    print("ingest:", pipeline.ingest(conn, cfg, iter_jobs(cfg)))
    print("score: ", pipeline.score_pending(conn, cfg, llm, args.limit))
    print("draft: ", pipeline.draft_pending(conn, cfg, llm, args.limit))


def cmd_queue(args):
    cfg, conn = _ctx(args)
    rows = conn.execute(
        """SELECT j.id, j.company, j.title, s.overall, s.decision, s.cv_id, a.status AS app
           FROM applications a JOIN jobs j ON j.id=a.job_id JOIN scores s ON s.job_id=j.id
           WHERE a.status IN ('drafted','needs_edit') ORDER BY s.overall DESC"""
    ).fetchall()
    if not rows:
        print("queue is empty")
    for r in rows:
        flag = "!" if r["app"] == "needs_edit" else " "
        print(f"{flag} {r['overall']:5.1f}  {r['decision']:<13} {r['cv_id'] or '-':<26} {r['company'][:22]:<22} {r['title'][:50]}\n         {r['id']}")


def _load(conn, prefix):
    job_id = db.resolve_job_id(conn, prefix)
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    score = conn.execute("SELECT * FROM scores WHERE job_id=?", (job_id,)).fetchone()
    app = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
    return job, score, app


def cmd_show(args):
    cfg, conn = _ctx(args)
    job, score, app = _load(conn, args.job)
    print(f"{job['title']} @ {job['company']}  [{job['location']}]\n{job['url']}")
    if score:
        print(f"\nfamily={score['family']} cv={score['cv_id']} score={score['overall']} decision={score['decision']}")
        for r in json.loads(score["reasons"] or "[]"):
            print(f"  - {r}")
    if app:
        issues = json.loads(app["verification"] or "{}").get("issues", [])
        print(f"\n--- letter ({app['status']}) ---\nSubject: {app['subject']}\n\n{app['letter']}")
        for i in issues:
            print(f"  ! {i}")


def cmd_edit(args):
    cfg, conn = _ctx(args)
    job, score, app = _load(conn, args.job)
    if not app:
        raise SystemExit("no draft for this job")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(app["letter"])
        path = f.name
    subprocess.call([os.environ.get("EDITOR", "nano"), path])
    text = Path(path).read_text(encoding="utf-8").strip()
    db.save_application(conn, job["id"], "drafted", app["subject"], text, {"issues": [], "edited_by_human": True})
    print("saved; marked as human-edited")


def cmd_approve(args):
    cfg, conn = _ctx(args)
    job, score, app = _load(conn, args.job)
    try:
        d = actions.approve(cfg, conn, job["id"], force=args.force)
    except actions.ActionError as e:
        raise SystemExit(f"{e} (see `show`; `edit` fixes it, --force approves anyway)")
    print(f"approved -> {d}")


def cmd_reject(args):
    cfg, conn = _ctx(args)
    job, _, app = _load(conn, args.job)
    actions.reject(conn, job["id"])
    print("rejected")


def cmd_send(args):
    cfg, conn = _ctx(args)
    rows = conn.execute(
        """SELECT j.id FROM applications a JOIN jobs j ON j.id=a.job_id
           WHERE a.status='approved'"""
    ).fetchall()
    mail = cfg.settings["mail"]
    sent_any = False
    for r in rows:
        try:
            prep = actions.prepare_application(cfg, conn, r["id"])
        except actions.ActionError as e:
            print(f"skip {r['id']}: {e}")
            continue
        if not args.yes:
            print(f"[dry-run] would send to {prep.to}: {prep.app['subject']} (attach {mailer.attachment_name(prep.cv, cfg.settings["mail"].get("attach_as"))})")
            continue
        if actions.cap_left(cfg, conn) <= 0:
            print(actions.cap_message(cfg))
            break
        if sent_any:
            time.sleep(mail["spacing_seconds"])
        actions.deliver_application(cfg, conn, prep)
        sent_any = True
        print(f"sent {r['id']}")


def cmd_try(args):
    """Judge one pasted posting. Reads a file, or stdin with '-'. Saves nothing, sends nothing."""
    cfg = load_config(args.home)
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    trial.require_posting_text(text)
    title = args.title or text.strip().splitlines()[0][:120]
    job = Job("manual", "try", args.company, title, "", args.location, None, text)
    print(trial.explain(LLM(cfg.settings), cfg, job, letter=not args.no_letter))


def cmd_selftest(args):
    """Send one clearly-labelled test application to yourself (or --to), through
    the real SMTP path, and register it so a reply to it is matched by `replies`."""
    cfg, conn = _ctx(args)
    mail = cfg.settings["mail"]
    to = args.to or mail["username"]
    subject = f"applybot selftest {time.strftime('%Y-%m-%d %H:%M')}"
    letter = ("Hello,\n\nThis is a test message from applybot. I have 20+ years of experience "
              "in backend engineering and distributed systems.\n\nBruno Sousa")
    att = Path(tempfile.gettempdir()) / "applybot-selftest-cv.txt"
    att.write_text("Placeholder CV attachment for the applybot self-test.\n", encoding="utf-8")
    msg = mailer.build_message(mail["username"], to, subject, letter, att)
    if not args.yes:
        print(f"[dry-run] would send '{subject}' from {mail['username']} to {to} with attachment {att.name}")
        return
    db.upsert_job(conn, Job("selftest", "1", "Selftest", "Senior Backend Engineer", "", "Remote", True,
                            "Self-test posting."), "scored")
    db.save_application(conn, "selftest:1", "sent", subject, letter, {"issues": []})
    reply = mailer.send(mail, msg)
    db.save_message(conn, message_id=msg["Message-ID"], job_id="selftest:1", direction="out",
                    from_addr=mail["username"], to_addr=to, subject=subject, body=letter, status="sent")
    saved = mailer.save_to_sent(mail, msg)
    print(f"server reply: {reply}")
    print(f"Message-ID:   {msg['Message-ID']}")
    print(f"sent to {to}. Copy in Sent folder: {'yes' if saved else 'NO (check mail.sent_folder)'}")


def cmd_replies(args):
    cfg, conn = _ctx(args)
    print("new replies matched:", replies.poll(conn, cfg))
    print(replies.process(conn, cfg, LLM(cfg.settings)))


def cmd_inbox(args):
    cfg, conn = _ctx(args)
    if args.n is None:
        hide = "" if args.all else "AND m.status NOT IN ('no_action','replied')"
        rows = conn.execute(
            f"""SELECT m.id, m.status, m.category, m.subject, j.company FROM messages m
                LEFT JOIN jobs j ON j.id=m.job_id WHERE m.direction='in' {hide} ORDER BY m.id DESC"""
        ).fetchall()
        if not rows:
            print("nothing in the list (use `inbox --all` to include handled messages)" if not args.all
                  else "no replies stored yet")
        for r in rows:
            print(f"#{r['id']:<4} {r['status']:<12} {r['category'] or '-':<20} {(r['company'] or '')[:20]:<20} {r['subject'][:50]}")
        return
    m = conn.execute("SELECT * FROM messages WHERE id=? AND direction='in'", (args.n,)).fetchone()
    if not m:
        raise SystemExit("no such message")
    print(f"From: {m['from_addr']}\nSubject: {m['subject']}\nStatus: {m['status']}  Category: {m['category']}")
    print(f"Summary: {m['summary']}\n\n{m['body']}")
    for why in json.loads(m["reasons"] or "[]"):
        print(f"  ! {why}")
    if m["draft_body"]:
        print(f"\n--- draft reply ---\nSubject: {m['draft_subject']}\n\n{m['draft_body']}")


def cmd_reply_edit(args):
    cfg, conn = _ctx(args)
    m = conn.execute("SELECT * FROM messages WHERE id=? AND direction='in'", (args.n,)).fetchone()
    if not m:
        raise SystemExit("no such message")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(m["draft_body"] or "")
        path = f.name
    subprocess.call([os.environ.get("EDITOR", "nano"), path])
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("empty reply, nothing saved")
    actions.save_reply(conn, m["id"], m["draft_subject"] or "", text)
    print("saved; marked as written by you")


def cmd_reply(args):
    cfg, conn = _ctx(args)
    try:
        m, to, msg = actions.prepare_reply(cfg, conn, args.n)
    except actions.ActionError as e:
        raise SystemExit(f"{e} (`reply-edit {args.n}` writes it)")
    if not args.yes:
        print(f"[dry-run] would reply to {to}: {m['draft_subject']}\n\n{m['draft_body']}")
        return
    actions.deliver_reply(cfg, conn, m, to, msg)
    print("sent")


def cmd_cvs(args):
    cfg, _ = _ctx(args)
    mapped = cfg.family_to_cv
    for cv in cfg.cvs:
        p = cfg.cv_file(cv["id"])
        print(f"{'ok ' if p.exists() else 'MISSING'}  {cv['id']:<28} {p.name}  <- {', '.join(cv['families'])}")
    print("\nfamilies without a CV (-> human review):")
    for f in cfg.family_names:
        if f not in mapped:
            print(f"  - {f}")


def cmd_stats(args):
    cfg, conn = _ctx(args)
    for label, q in (
        ("jobs by status", "SELECT status k, COUNT(*) n FROM jobs GROUP BY status"),
        ("decisions", "SELECT decision k, COUNT(*) n FROM scores GROUP BY decision"),
        ("applications", "SELECT status k, COUNT(*) n FROM applications GROUP BY status"),
    ):
        print(label + ":", {r["k"]: r["n"] for r in conn.execute(q)})


def cmd_serve(args):
    from . import web
    cfg = load_config(args.home)
    web.serve(cfg.root, args.port, args.open)


def main(argv=None):
    p = argparse.ArgumentParser(prog="applybot", description="Local, review-first job application assistant")
    p.add_argument("--home", default=None, help="project root (default: auto)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn, lim in (("ingest", cmd_ingest, False), ("score", cmd_score, True),
                          ("draft", cmd_draft, True), ("run", cmd_run, True)):
        s = sub.add_parser(name)
        if lim:
            s.add_argument("--limit", type=int, default=None)
        s.set_defaults(fn=fn)
    s = sub.add_parser("serve", help="local web dashboard on 127.0.0.1 (try a posting, review, send, inbox)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open it in your browser")
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("queue").set_defaults(fn=cmd_queue)
    sub.add_parser("cvs").set_defaults(fn=cmd_cvs)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    for name, fn in (("show", cmd_show), ("edit", cmd_edit), ("reject", cmd_reject)):
        s = sub.add_parser(name)
        s.add_argument("job", help="job id or unique prefix")
        s.set_defaults(fn=fn)
    s = sub.add_parser("approve")
    s.add_argument("job")
    s.add_argument("--force", action="store_true", help="approve a draft that failed verification")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("send")
    s.add_argument("--yes", action="store_true", help="actually send (default is dry-run)")
    s.set_defaults(fn=cmd_send)
    s = sub.add_parser("try", help="judge ONE pasted posting (file, or '-' for stdin): CV, score, letter. Saves/sends nothing")
    s.add_argument("file")
    s.add_argument("--title", default=None, help="default: first line of the text")
    s.add_argument("--company", default="")
    s.add_argument("--location", default="")
    s.add_argument("--no-letter", action="store_true", help="skip the letter and its fact-check (cheaper)")
    s.set_defaults(fn=cmd_try)
    s = sub.add_parser("selftest", help="send one test application to yourself (dry-run unless --yes)")
    s.add_argument("--to", default=None, help="default: your own address")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_selftest)
    sub.add_parser("replies", help="poll IMAP for answers to sent applications, classify, draft").set_defaults(fn=cmd_replies)
    s = sub.add_parser("inbox", help="list replies waiting, or show one by number")
    s.add_argument("n", nargs="?", type=int)
    s.add_argument("--all", action="store_true", help="include handled messages (no_action, replied)")
    s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("reply-edit", help="write or fix the reply to message N in $EDITOR")
    s.add_argument("n", type=int)
    s.set_defaults(fn=cmd_reply_edit)
    s = sub.add_parser("reply", help="send the drafted reply to message N (dry-run unless --yes)")
    s.add_argument("n", type=int)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_reply)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
