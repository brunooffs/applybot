"""Local dashboard: a small web page on 127.0.0.1 for the things you do by hand.

It is a front end for the same functions the command line uses (`actions`,
`trial`, `replies`); it has no decision logic of its own. Standard library only.

Safety:
  * listens on 127.0.0.1 only, and refuses requests whose Host header is not that
    address (blocks DNS-rebinding from a web page you happen to have open);
  * every POST needs a random per-run token and, if the browser sends one, a
    matching Origin (blocks another site from posting to localhost);
  * everything shown from a posting or an email is HTML-escaped;
  * sending always goes through a confirmation page, one message at a time.
"""
from __future__ import annotations

import collections
import html
import json
import os
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import actions, cvstore, db, factsimport, history, profileimport, replies, runner, scoring, trial
from .config import load_config
from .llm import LLM
from .models import Job
from .scoring import effective_status

esc = html.escape
SEND_LOCK = threading.Lock()     # one send at a time: a double click cannot send twice
MAX_BODY = 1_000_000
MAX_UPLOAD = 24_000_000   # CV uploads (the two upload paths below only)
UPLOAD_PATHS = ("/facts/upload", "/profile/upload")

# Results of "try", kept in memory so you can come back to one, and save it to the review queue.
# Lost when the server stops; the review queue itself is in the database.
RESULTS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
RESULTS_LOCK = threading.Lock()
RESULTS_MAX = 15


def stash_result(job, res, form) -> str:
    rid = secrets.token_urlsafe(6)
    with RESULTS_LOCK:
        RESULTS[rid] = {"job": job, "res": res, "form": form, "at": time.time(), "saved": None}
        while len(RESULTS) > RESULTS_MAX:
            RESULTS.popitem(last=False)
    return rid


def get_result(rid: str):
    with RESULTS_LOCK:
        return RESULTS.get(rid)


def make_http():                 # hook for tests: an httpx client for "check this source" (None = real network)
    return None


def make_llm(cfg):               # a module-level hook so tests can swap in a fake
    return LLM(cfg.settings)


class Redirect(Exception):
    def __init__(self, to: str):
        self.to = to


class NotFound(Exception):
    pass


CSS = """
:root{color-scheme:light dark;--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--line:#dcdcd5;--card:#fff;--acc:#1f5fbf;--ok:#1e7a3c;--bad:#b3261e;--warn:#9a6700}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecece8;--mut:#9a9a93;--line:#34342f;--card:#1e1e1c;--acc:#7fb0ff;--ok:#5fcf84;--bad:#ff8a80;--warn:#e3b341}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{border-bottom:1px solid var(--line);background:var(--card)}nav{max-width:1000px;margin:0 auto;padding:0 20px;display:flex;gap:4px;align-items:center;flex-wrap:wrap}
nav b{margin-right:16px}nav a{padding:14px 12px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent}
nav a.on,nav a:hover{color:var(--fg);border-bottom-color:var(--acc)}main{max-width:1000px;margin:0 auto;padding:24px 20px 60px}
nav details.dd{position:relative;margin:0}nav details.dd>summary{list-style:none;cursor:pointer;padding:14px 12px;color:var(--mut);border-bottom:2px solid transparent}
nav details.dd>summary::-webkit-details-marker{display:none}nav details.dd>summary::after{content:" ▾";font-size:11px}
nav details.dd.on>summary,nav details.dd[open]>summary,nav details.dd>summary:hover{color:var(--fg)}nav details.dd.on>summary{border-bottom-color:var(--acc)}
nav details.dd>div{position:absolute;left:0;top:100%;z-index:10;min-width:180px;display:flex;flex-direction:column;background:var(--card);
border:1px solid var(--line);border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.15);padding:4px 0}
nav details.dd>div a{padding:8px 16px;border-bottom:0}nav details.dd>div a.on,nav details.dd>div a:hover{background:var(--bg)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.sub{color:var(--mut);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.grid .card{margin:0}
.num{font-size:28px;font-weight:600}.mut{color:var(--mut)}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:500;font-size:13px}
td.n{text-align:right;white-space:nowrap}a{color:var(--acc)}input[type=text],textarea{width:100%;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
textarea{font-family:ui-monospace,Menlo,monospace;font-size:13.5px;line-height:1.5}label{display:block;margin:12px 0 4px;font-size:13px;color:var(--mut)}
button,.btn{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button.pri,.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.danger{color:var(--bad)}button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:var(--ok)}.badge.bad{color:var(--bad);border-color:var(--bad)}.badge.warn{color:var(--warn);border-color:var(--warn)}
.flash{padding:10px 14px;border-radius:6px;margin:0 0 16px;border:1px solid}.flash.ok{color:var(--ok);border-color:var(--ok)}.flash.err{color:var(--bad);border-color:var(--bad)}
pre{white-space:pre-wrap;word-wrap:break-word;margin:0;font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.5}
.tabs{display:flex;gap:8px;margin:10px 0 14px}.tabs a{padding:5px 12px;border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--mut)}.tabs a.on{color:var(--fg);border-color:var(--acc)}
.hero{display:flex;gap:24px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.verdict{font-size:20px;font-weight:600}.verdict.ok{color:var(--ok)}.verdict.warn{color:var(--warn)}.verdict.bad{color:var(--bad)}
.scorebox{min-width:260px;flex:1;max-width:380px}.scorebox .num{line-height:1}.bar{position:relative;height:8px;border-radius:4px;background:var(--line);margin-top:8px}
.bar>i{display:block;height:100%;border-radius:4px;background:var(--acc)}.bar.ok>i{background:var(--ok)}.bar.warn>i{background:var(--warn)}.bar.bad>i{background:var(--bad)}
.bar>u{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--fg);opacity:.35}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.chip{padding:2px 10px;border-radius:12px;font-size:13px;border:1px solid var(--line);background:var(--bg)}
.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.bad{color:var(--bad);border-color:var(--bad)}.chip.warn{color:var(--warn);border-color:var(--warn)}
.dims{display:grid;grid-template-columns:150px 1fr 36px;gap:6px 12px;align-items:center}.dims .bar{margin:0}.dims span{font-size:13px}.dims .v{text-align:right;font-variant-numeric:tabular-nums}
.req{display:grid;grid-template-columns:28px 1fr;gap:2px 8px;padding:9px 0;border-bottom:1px solid var(--line)}.req:last-child{border-bottom:0}
.ico{font-weight:700;font-size:17px;line-height:1.3;text-align:center}.ico.ok{color:var(--ok)}.ico.bad{color:var(--bad)}.ico.warn{color:var(--warn)}
.req .q{font-style:italic;color:var(--mut);font-size:13px}.req .note{font-size:12.5px;color:var(--warn)}
ins{background:rgba(46,160,67,.2);text-decoration:none}del{background:rgba(220,53,69,.2)}
.letter{font-family:Georgia,'Times New Roman',serif;font-size:15.5px;line-height:1.65;white-space:pre-wrap}
.banner{padding:10px 14px;border-radius:6px;border:1px solid;margin-bottom:12px}.banner.ok{color:var(--ok);border-color:var(--ok)}.banner.bad{color:var(--bad);border-color:var(--bad)}
details{margin:10px 0}summary{cursor:pointer;color:var(--mut)}ul{margin:6px 0;padding-left:20px}.issue{color:var(--bad)}
"""

APPLICATIONS = (("try", "/try", "Try a posting"), ("postings", "/postings", "Postings"), ("review", "/queue", "Review"),
                ("approved", "/queue?tab=approved", "Approved"), ("inbox", "/inbox", "Inbox"), ("history", "/history", "History"))
# top bar: a plain link, or ("group", label, items) for a dropdown
NAV = (("overview", "/", "Overview"), ("group", "Applications", APPLICATIONS), ("facts", "/facts", "Facts"),
       ("profile", "/profile", "Profile"), ("sources", "/sources", "Sources"), ("run", "/run", "Run"),
       ("settings", "/settings", "Models"))


def active_for(path: str, q: dict) -> str:
    if path == "/try":
        return "try"
    if path == "/settings":
        return "settings"
    if path in ("/sources", "/sources/save", "/sources/check"):
        return "sources"
    if path in ("/facts", "/facts/upload", "/facts/save", "/facts/discard"):
        return "facts"
    if path in ("/profile", "/profile/upload", "/profile/save", "/profile/discard"):
        return "profile"
    if path in ("/run", "/run/start", "/run/cancel"):
        return "run"
    if path == "/history":
        return "history"
    if path == "/postings":
        return "postings"
    if path in ("/inbox", "/message", "/reply"):
        return "inbox"
    if path in ("/queue", "/job", "/send"):
        return "approved" if q.get("tab") == "approved" else "review"
    return "overview"


def link(path: str, **params) -> str:
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return path + ("?" + urlencode(params) if params else "")


def badge(status: str) -> str:
    cls = {"needs_edit": "bad", "bounced": "bad", "needs_you": "bad", "rejected": "bad", "reject": "bad",
           "drafted": "warn", "human": "warn", "draft_ready": "warn", "new": "warn",
           "approved": "ok", "sent": "ok", "replied": "ok", "auto_eligible": "ok"}.get(status, "")
    return f'<span class="badge {cls}">{esc(status or "-")}</span>'


def safe_link(url: str, text: str | None = None) -> str:
    if url and urlparse(url).scheme in ("http", "https"):
        return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(text or url)}</a>'
    return esc(url or "")


def layout(body: str, nonce: str, title: str, active: str, ok: str = "", err: str = "") -> str:
    def item(key, href, label):
        return f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
    nav = "".join(
        item(*e) if e[0] != "group" else
        f'<details class="dd{" on" if any(k == active for k, _, _ in e[2]) else ""}"><summary>{e[1]}</summary>'
        f'<div>{"".join(item(*i) for i in e[2])}</div></details>' for e in NAV)
    flash = (f'<div class="flash ok">{esc(ok)}</div>' if ok else "") + (f'<div class="flash err">{esc(err)}</div>' if err else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{"<meta http-equiv=refresh content=3>" if "<!--refresh-->" in body else ""}'
            f'<title>{esc(title)} · applybot</title><style>{CSS}</style></head><body>'
            f'<header><nav><b>applybot</b>{nav}</nav></header><main>{flash}{body}</main>'
            f'<script nonce="{nonce}">for(const f of document.querySelectorAll("form[data-busy]"))f.addEventListener("submit",()=>{{'
            f'for(const b of f.querySelectorAll("button"))setTimeout(()=>{{b.disabled=true}},0);'
            f'const s=f.querySelector(".busy");if(s)s.hidden=false}});'
            f'const dd=document.querySelector("nav details.dd");if(dd){{document.addEventListener("click",e=>{{if(!dd.contains(e.target))dd.open=false}});'
            f'document.addEventListener("keydown",e=>{{if(e.key==="Escape")dd.open=false}})}}'
            f'for(const b of document.querySelectorAll("[data-copy]"))b.addEventListener("click",()=>{{'
            f'navigator.clipboard.writeText(document.querySelector(b.dataset.copy).textContent).then(()=>{{b.textContent="Copied"}})}});</script></body></html>')


def hidden(tok: str, **fields) -> str:
    return f'<input type="hidden" name="_t" value="{esc(tok)}">' + "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in fields.items())


# --- pages ------------------------------------------------------------------

def _models(cfg) -> str:
    try:
        return make_llm(cfg).describe()
    except Exception as e:
        return f"settings problem: {e}"


def page_overview(ctx):
    cfg, conn, tok = ctx["cfg"], ctx["conn"], ctx["tok"]

    def n(sql, *a):
        return conn.execute(sql, a).fetchone()[0]

    review = n("SELECT COUNT(*) FROM applications WHERE status IN ('drafted','needs_edit')")
    failed = n("SELECT COUNT(*) FROM applications WHERE status='needs_edit'")
    approved = n("SELECT COUNT(*) FROM applications WHERE status='approved'")
    sent = n("SELECT COUNT(*) FROM applications WHERE status='sent'")
    inbox = n("SELECT COUNT(*) FROM messages WHERE direction='in' AND status NOT IN ('no_action','replied')")
    today, cap = db.count_sent_today(conn), cfg.settings["mail"]["daily_cap"]
    dec = {r["k"]: r["c"] for r in conn.execute("SELECT decision k, COUNT(*) c FROM scores GROUP BY decision")}
    st = {r["k"]: r["c"] for r in conn.execute("SELECT status k, COUNT(*) c FROM jobs GROUP BY status")}
    card = lambda label, num, href, extra="": (f'<div class="card"><div class="mut">{label}</div><div class="num">'
                                               f'<a href="{href}" style="text-decoration:none">{num}</a></div><div class="mut">{extra}</div></div>')
    body = (
        "<h1>Overview</h1><p class='sub'>Everything here is local. Nothing is sent without a confirmation page.</p>"
        '<div class="grid">'
        + card("To review", review, "/queue", f"{failed} failed the fact-check" if failed else "")
        + card("Approved, not sent", approved, "/queue?tab=approved")
        + card("Replies needing you", inbox, "/inbox")
        + card("Sent today", f"{today}/{cap}", "/queue?tab=sent", f"{sent} sent in total")
        + "</div>"
        "<h2>Check for replies</h2><div class='card'><p class='mut' style='margin-top:0'>Reads your mailbox, matches answers to your "
        "applications, classifies them and drafts replies. Needs the mail password in the environment this server was started from.</p>"
        f'<form method="post" action="/replies/check" data-busy>{hidden(tok)}<button class="pri">Check replies now</button> '
        '<span class="busy mut" hidden>Working…</span></form></div>'
        f"<h2>Pipeline</h2><div class='card mut'>Jobs: {esc(json.dumps(st))}<br>Decisions: {esc(json.dumps(dec))}<br>"
        f"Models: {esc(_models(cfg))}<br>New postings arrive with <code>applybot run</code> in a terminal.</div>"
        + _file_panels(cfg)
    )
    return "Overview", body


def _file_panels(cfg) -> str:
    """The three files that steer everything, read-only, collapsed. Raw text, comments included."""
    from .config import DEFAULTS
    out = ("<h2>Your files</h2><p class='mut'>What the bot is working from right now. Read-only here: edit the file, or "
           "rebuild it from your CV on the Facts and Profile pages.</p>")
    for label, rel in (("Criteria", "config/criteria.yaml"), ("Facts", "config/facts.yaml"), ("Profile", "config/profile.yaml")):
        path = cfg.root / rel
        note = ""
        if not path.exists() and rel.endswith("criteria.yaml") and (DEFAULTS / rel).exists():
            path, note = DEFAULTS / rel, " (you have no criteria.yaml of your own, so the packaged default is in use)"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = None
        shown = (f"<pre style='max-height:460px;overflow:auto;margin:8px 0 0;white-space:pre-wrap'>{esc(text)}</pre>"
                 if text is not None else "<p class='mut'>Not created yet.</p>")
        out += (f"<details class='card'><summary><b>{label}</b> <span class='mut'>{esc(rel)}{esc(note)}</span></summary>"
                f"{shown}</details>")
    return out


def _bar(value, cls: str = "", ticks=()) -> str:
    try:
        v = max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return '<div class="bar"></div>'
    t = "".join(f'<u style="left:{float(x)}%" title="{x}"></u>' for x in ticks)
    return f'<div class="bar {cls}"><i style="width:{v}%"></i>{t}</div>'


def _band(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "ok" if v >= 80 else "warn" if v >= 60 else "bad"


ICON = {"met": ("✓", "ok", "Met"), "unmet": ("✗", "bad", "Not met"), "unknown": ("?", "warn", "Cannot confirm")}


def render_trial(res: dict, after_hero: str = "") -> str:
    """The result of `try` as a page. Every value is escaped; the numbers come from the same data
    as the terminal output, nothing is decided here."""
    label, cls = {"auto_eligible": ("Strong match", "ok"), "human": ("Needs your review", "warn"),
                  "reject": ("Rejected", "bad")}.get(res["decision"], (res["decision"], ""))
    th = res.get("thresholds") or {}
    ticks = [th[k] for k in ("potential", "good", "strong") if k in th]
    cv = (f'<span class="chip {"ok" if res["cv_found"] else "bad"}">CV: {esc(res["cv_file"] or res["cv_id"])}'
          f'{"" if res["cv_found"] else " (file missing)"}</span>' if res["cv_id"] else '<span class="chip warn">no CV mapped</span>')
    arr = res["arrangement"]
    ptl = scoring.eligible_value(arr) if arr else "?"
    chips = (cv + f'<span class="chip">{esc(res["family"])}</span>'
             f'<span class="chip {"ok" if ptl == "yes" else "bad" if ptl == "no" else "warn"}">Eligible: {esc(str(ptl))}</span>'
             f'<span class="chip">{esc(str(arr.get("remote", "?")))}</span><span class="chip">{esc(str(arr.get("employment_type", "?")))}</span>'
             f'<span class="chip">language: {esc(res["language"])}</span>'
             f'<span class="chip">{esc(res["contact"]) if res["contact"] else "no email: apply via the posting"}</span>')
    out = [
        f'<div class="card"><div class="hero"><div><div class="verdict {cls}">{esc(label)}</div>'
        f'<div>{esc(res["title"])}{" @ " + esc(res["company"]) if res["company"] else ""}</div>'
        f'<div class="chips">{chips}</div></div>'
        f'<div class="scorebox"><div class="num">{res["overall"]}<span class="mut" style="font-size:14px"> / 100</span></div>'
        f'{_bar(res["overall"], cls, ticks)}'
        f'<div class="mut" style="font-size:12px;margin-top:4px">markers: {" · ".join(str(t) for t in ticks)}</div></div></div></div>']
    out.append(after_hero)
    if res["reasons"]:
        out.append("<div class='card'><b>Why</b><ul>" + "".join(f"<li>{esc(r)}</li>" for r in res["reasons"]) + "</ul></div>")
    dims = "".join(f'<span>{esc(d.replace("_", " "))}</span>{_bar(v, _band(v))}<span class="v">{"-" if v is None else esc(str(v))}</span>'
                   for d, v in res["dimensions"].items())
    out.append(f'<h2>Scores</h2><div class="card"><div class="dims">{dims}</div></div>')
    if res["requirements"]:
        rows = []
        for r in res["requirements"]:
            ic, ic_cls, ic_txt = ICON.get(r["effective"], ICON["unknown"])
            said = f'{r["status"]} on {r["basis"]}'
            note = ""
            if r["status"] != r["effective"]:
                note = f'<div class="note">The model said “{esc(said)}”, which the rules count as “{esc(r["effective"])}”.</div>'
            hard = ' <span class="badge bad">hard requirement</span>' if r["disqualifying"] else ""
            quote = f'<div class="q">“{esc(r["evidence"])}”</div>' if r["evidence"] else ""
            rows.append(f'<div class="req"><div class="ico {ic_cls}" title="{ic_txt}">{ic}</div>'
                        f'<div>{esc(r["requirement"])}{hard}{quote}{note}</div></div>')
        out.append('<h2>Mandatory requirements</h2><div class="card">' + "".join(rows) + "</div>")
    if arr.get("evidence"):
        out.append(f'<div class="card mut"><b>Work arrangement</b><br><i>“{esc(str(arr["evidence"])[:160])}”</i></div>')

    st = res["letter_state"]
    if st == "off":
        out.append("<p class='mut'>Letter skipped (score only).</p>")
    elif st == "skipped":
        out.append("<p class='mut'>No letter: rejected jobs do not get one.</p>")
    else:
        L = res["letter"]
        note = f" after {L['revisions']} automatic revision(s)" if L["revisions"] else ""
        if L["issues"]:
            banner = (f'<div class="banner bad"><b>Fact-check failed{esc(note)}.</b> It would be queued as “needs_edit”.<ul>'
                      + "".join(f"<li>{esc(i)}</li>" for i in L["issues"]) + "</ul></div>")
        else:
            banner = f'<div class="banner ok"><b>Fact-check passed{esc(note)}.</b> It would be queued as “drafted”.</div>'
        out.append(f'<h2>Letter ({esc(L["lang"])})</h2>{banner}<div class="card"><div class="mut">Subject</div>'
                   f'<div style="font-weight:600;margin-bottom:12px">{esc(L["subject"])}</div>'
                   f'<div class="letter" id="letter-body">{esc(L["body"])}</div>'
                   '<div class="row"><button type="button" data-copy="#letter-body">Copy letter</button></div></div>')
    sec = res["seconds"]
    parts = [f"score {sec['score']:.0f}s"] + ([f"letter and fact-check {sec['letter']:.0f}s"] if "letter" in sec else [])
    out.append(f"<p class='mut' style='font-size:13px'>Models: {esc(res['models'])} · {' · '.join(parts)} · total {sec['total']:.0f}s</p>")
    return "".join(out)


def page_try(ctx, result: dict | None = None, form: dict | None = None, r_id: str = ""):
    tok = ctx["tok"]
    f = form or {}
    body = (
        "<h1>Try a posting</h1><p class='sub'>Paste the full text of one posting. It is scored and a letter is drafted and "
        "fact-checked, like the real pipeline. Nothing is saved and nothing is sent.</p>"
        f'<form method="post" action="/try" data-busy>{hidden(tok)}'
        f'<label>Posting text</label><textarea name="posting" rows="14" required>{esc(f.get("posting", ""))}</textarea>'
        '<div class="grid"><div><label>Title (optional, default: first line)</label>'
        f'<input type="text" name="title" value="{esc(f.get("title", ""))}"></div>'
        f'<div><label>Company (optional)</label><input type="text" name="company" value="{esc(f.get("company", ""))}"></div>'
        f'<div><label>Location (optional)</label><input type="text" name="location" value="{esc(f.get("location", ""))}"></div>'
        f'<div><label>Posting URL (optional, shown in review)</label><input type="text" name="url" value="{esc(f.get("url", ""))}"></div></div>'
        f'<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="no_letter" value="1" '
        f'{"checked" if f.get("no_letter") else ""}> Score only, skip the letter and its fact-check (cheaper and faster)</label>'
        '<div class="row"><button class="pri">Run</button><span class="busy mut" hidden>Working… a local model can take a minute or two.</span></div></form>'
    )
    if result:
        body += "<h2>Result</h2>" + render_trial(result, _save_box(tok, r_id, result))
    with RESULTS_LOCK:
        recent = list(RESULTS.items())[::-1]
    if recent:
        rows = "".join(
            f'<tr><td class="n">{x["res"]["overall"]}</td><td>{badge(x["res"]["decision"])}</td>'
            f'<td><a href="{link("/try", r=rid)}">{esc(x["res"]["title"][:80])}</a></td><td class="mut">{esc(x["res"]["company"])}</td>'
            f'<td class="mut">{time.strftime("%H:%M", time.localtime(x["at"]))}</td>'
            f'<td>{"in review" if x["saved"] else ""}</td></tr>' for rid, x in recent)
        body += ("<h2>Recent results</h2><div class='card'><table>" + rows + "</table>"
                 "<div class='mut' style='margin-top:8px;font-size:13px'>Kept until you stop the server.</div></div>")
    return "Try a posting", body


def _save_box(tok: str, r_id: str, res: dict) -> str:
    """The step from a try result to the review queue."""
    item = get_result(r_id) if r_id else None
    if item and item["saved"]:
        return ("<div class='card'><span class='badge ok'>in review queue</span> "
                f"<a href='{link('/job', id=item['saved'])}'>Open it in Review</a></div>")
    if res["letter_state"] != "done" or res["decision"] == "reject":
        why = ("No letter was made, so there is nothing to review. Run it again without “score only”."
               if res["letter_state"] == "off" else "Rejected postings are not queued.")
        return f"<div class='card mut'>{why}</div>"
    status = "needs editing, the fact-check failed" if res["letter"]["issues"] else "ready to review"
    return ('<div class="card"><form method="post" action="/try/save" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">'
            f'{hidden(tok, r=r_id)}<button class="pri">Add to review queue</button>'
            f'<span class="mut">The letter is {status}. Nothing is sent from here.</span></form></div>')


def page_try_view(ctx, q):
    item = get_result(q.get("r", ""))
    if q.get("r") and not item:
        return page_try(ctx)    # the server was restarted, or the result aged out
    if item:
        return page_try(ctx, result=item["res"], form=item["form"], r_id=q["r"])
    return page_try(ctx)


QUEUE_TABS = {
    "review": ("Review", ("drafted", "needs_edit"), "Drafts waiting for you. Failed fact-checks are marked and cannot be approved without an edit."),
    "approved": ("Approved", ("approved",), "Approved applications. Sending is a separate step with its own confirmation."),
    "sent": ("Sent", ("sent", "bounced"), "Applications already sent (or marked as applied by hand)."),
    "rejected": ("Rejected", ("rejected",), "Drafts you turned down."),
}


def page_queue(ctx, q):
    conn = ctx["conn"]
    tab = q.get("tab", "review")
    if tab not in QUEUE_TABS:
        tab = "review"
    name, statuses, blurb = QUEUE_TABS[tab]
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"""SELECT j.id, j.company, j.title, s.overall, s.decision, s.cv_id, a.status AS app, a.updated_at
            FROM applications a JOIN jobs j ON j.id=a.job_id LEFT JOIN scores s ON s.job_id=j.id
            WHERE a.status IN ({marks}) ORDER BY (a.status='needs_edit') DESC, s.overall DESC""", statuses).fetchall()
    tabs = "".join(f'<a class="{"on" if t == tab else ""}" href="{link("/queue", tab=t)}">{QUEUE_TABS[t][0]}</a>' for t in QUEUE_TABS)
    if rows:
        trs = "".join(
            f'<tr><td class="n">{r["overall"] if r["overall"] is not None else "-"}</td><td>{badge(r["decision"])}</td>'
            f'<td>{esc(r["company"] or "")}</td><td><a href="{link("/job", id=r["id"])}">{esc(r["title"] or "")}</a></td>'
            f'<td class="mut">{esc(r["cv_id"] or "no CV")}</td><td>{badge(r["app"])}</td></tr>' for r in rows)
        table = f"<table><tr><th>Score</th><th>Decision</th><th>Company</th><th>Role</th><th>CV</th><th>Status</th></tr>{trs}</table>"
    else:
        table = "<div class='card mut'>Nothing here.</div>"
    return name, f"<h1>{esc(name)}</h1><p class='sub'>{esc(blurb)}</p><div class='tabs'>{tabs}</div>{table}"


def page_job(ctx, q):
    cfg, conn, tok = ctx["cfg"], ctx["conn"], ctx["tok"]
    job_id = q.get("id", "")
    try:
        job, score, app = actions.load(conn, job_id)
    except actions.ActionError:
        raise NotFound()
    parts = [f"<h1>{esc(job['title'] or '')}</h1><p class='sub'>{esc(job['company'] or '')} · {esc(job['location'] or '')}"
             f" · {safe_link(job['url'], 'open posting')}</p>"]
    if score:
        cvf = cfg.cv_file(score["cv_id"]) if score["cv_id"] else None
        cv = (f"{esc(score['cv_id'])} → {esc(cvf.name)} " + ('<span class="badge ok">found</span>' if cvf.exists()
              else '<span class="badge bad">file missing</span>')) if cvf else "none mapped (human review)"
        reasons = "".join(f"<li>{esc(r)}</li>" for r in json.loads(score["reasons"] or "[]"))
        parts.append(
            f'<div class="card"><b>{score["overall"]}</b> · {badge(score["decision"])} · family: {esc(score["family"] or "-")}<br>'
            f'<span class="mut">CV:</span> {cv}'
            f"<ul>{reasons}</ul>"
            f'<form method="post" action="/job/action" class="row" style="margin-top:4px">{hidden(tok, id=job_id)}'
            f'<label style="margin:0">Send applications to</label>'
            f'<input type="text" name="contact_email" size="34" value="{esc(score["contact_email"] or "")}" placeholder="no email in the posting">'
            '<button name="action" value="contact">Save email</button></form>'
            f"<span class='mut'>{'Found in the posting, or set by you.' if score['contact_email'] else 'Without an email you apply through the posting page.'}"
            " Applications are only sent to this address.</span></div>")
        try:
            raw = json.loads(score["raw"] or "{}")
        except ValueError:
            raw = {}
        reqs = raw.get("mandatory_requirements") or []
        if reqs:
            trs = "".join(
                f'<tr><td>{"★ " if r.get("disqualifying") else ""}{esc(str(r.get("requirement", "?")))}</td>'
                f'<td>{esc(str(r.get("status")))} on {esc(str(r.get("basis")))}</td><td>{badge(effective_status(r))}</td>'
                f'<td class="mut">{esc(str(r.get("evidence") or ""))}</td></tr>' for r in reqs)
            parts.append("<details><summary>Mandatory requirements and the model's evidence</summary>"
                         f"<table><tr><th>Requirement (★ = disqualifying)</th><th>Model said</th><th>Counted as</th><th>Quote</th></tr>{trs}</table></details>")
    parts.append(f"<details><summary>Posting text</summary><div class='card'><pre>{esc((job['description'] or '')[:30000])}</pre></div></details>")

    if not app:
        parts.append("<div class='card mut'>No draft exists for this job.</div>")
        return job["title"] or "Job", "".join(parts)

    try:
        issues = json.loads(app["verification"] or "{}").get("issues", [])
    except ValueError:
        issues = []
    edited = '<span class="badge ok">edited by you</span>' if '"edited_by_human": true' in (app["verification"] or "") else ""
    parts.append(f"<h2>Letter {badge(app['status'])} {edited}</h2>")
    if issues:
        parts.append("<div class='card'><b>Fact-check found problems</b><ul>" + "".join(f"<li class='issue'>{esc(i)}</li>" for i in issues)
                     + "</ul><span class='mut'>Fix or delete the flagged claims, then save. Approval stays blocked until none remain.</span></div>")
    st = app["status"]
    if st in ("drafted", "needs_edit"):
        parts.append(
            f'<form method="post" action="/job/action">{hidden(tok, id=job_id)}'
            f'<label>Subject</label><input type="text" name="subject" value="{esc(app["subject"] or "")}">'
            f'<label>Letter</label><textarea name="letter" rows="20">{esc(app["letter"] or "")}</textarea>'
            '<div class="row">'
            '<button name="action" value="save">Save edits</button>'
            '<button class="pri" name="action" value="approve">Approve</button>'
            '<button class="danger" name="action" value="reject">Reject</button></div></form>'
            "<p class='mut'>Saving re-runs the automatic checks (numbers, copied posting wording, not-claimed skills). A letter with remaining flags cannot be approved.</p>")
    else:
        parts.append(f'<div class="card"><b>{esc(app["subject"] or "")}</b><pre id="letter-text" style="margin-top:8px">{esc(app["letter"] or "")}</pre></div>')
        if st == "approved":
            can_mail = bool(score and score["contact_email"])
            cv_file = cfg.cv_file(score["cv_id"]) if score and score["cv_id"] else None
            row = (f'<a class="btn pri" href="{link("/send", id=job_id)}">Review and send…</a>' if can_mail else
                   (f"{safe_link(job['url'], 'Open the application page')} "
                    "<button type='button' data-copy='#letter-text'>Copy letter</button> "
                    f"<span class='mut'>Upload {esc(cv_file.name) if cv_file else 'your CV'} (also in the folder below), then mark it.</span>"))
            parts.append(
                f'<div class="row">{row}'
                f'<form method="post" action="/job/action" style="display:inline">{hidden(tok, id=job_id)}'
                '<button name="action" value="applied">Mark as applied by hand</button> '
                '<button class="danger" name="action" value="reject">Reject</button></form></div>'
                f"<p class='mut'>Application folder: {esc(app['packet_dir'] or '-')}</p>")
    return job["title"] or "Job", "".join(parts)


def page_send(ctx, q):
    cfg, conn, tok = ctx["cfg"], ctx["conn"], ctx["tok"]
    job_id = q.get("id", "")
    try:
        prep = actions.prepare_application(cfg, conn, job_id)
    except actions.ActionError as e:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise NotFound()
        return "Cannot send", (f"<h1>Cannot send</h1><div class='flash err'>{esc(str(e))}</div>"
                               f"<a class='btn' href='{link('/job', id=job_id)}'>Back</a>")
    left = actions.cap_left(cfg, conn)
    cap_note = (f"<div class='flash err'>{esc(actions.cap_message(cfg))}</div>" if left <= 0
                else f"<p class='mut'>{left} of {cfg.settings['mail']['daily_cap']} sends left today.</p>")
    send_btn = ("" if left <= 0 else
                f'<form method="post" action="/send" data-busy style="display:inline">{hidden(tok, id=job_id)}'
                '<button class="pri">Send now</button> <span class="busy mut" hidden>Sending…</span></form>')
    body = (
        "<h1>Send this application?</h1><p class='sub'>This sends a real email from your own server. It cannot be undone.</p>"
        f'<div class="card"><table><tr><th>From</th><td>{esc(cfg.settings["mail"]["username"])}</td></tr>'
        f'<tr><th>To</th><td><b>{esc(prep.to)}</b></td></tr><tr><th>Subject</th><td>{esc(prep.app["subject"])}</td></tr>'
        f'<tr><th>Attachment</th><td>{esc(next(prep.msg.iter_attachments()).get_filename())} <span class="mut">(file: {esc(prep.cv.name)})</span></td></tr><tr><th>Role</th><td>{esc(prep.job["title"])} @ {esc(prep.job["company"])}</td></tr></table></div>'
        f'<div class="card"><pre>{esc(prep.app["letter"])}</pre></div>{cap_note}'
        f'<div class="row">{send_btn}<a class="btn" href="{link("/job", id=job_id)}">Cancel</a></div>')
    return "Send", body


STAGE_HELP = {
    "score": "Reads the posting and rates the fit. A stronger model gives steadier scores.",
    "draft": "Writes the first version of the letter.",
    "revise": "Rewrites a letter after the fact-check flagged claims.",
    "verify": "The fact-check that stops invented claims. Worth a strong model.",
    "reply": "Classifies recruiter replies and drafts answers.",
    "extract": "Reads your CV on the Facts and Profile pages and proposes facts and a profile. Ollama keeps the CV on this machine; Anthropic is better at it.",
}


def page_settings(ctx, q):
    from .llm import PROVIDERS, STAGES
    cfg, tok = ctx["cfg"], ctx["tok"]
    llm = cfg.settings.get("llm", {})
    default, stages = llm.get("provider", "ollama"), llm.get("stages") or {}
    models = {"ollama": (llm.get("ollama") or {}).get("model", "?"), "anthropic": (llm.get("anthropic") or {}).get("model", "?")}
    rows = ""
    for st in STAGES:
        cur = stages.get(st, default)
        opts = "".join(f'<option value="{p}"{" selected" if p == cur else ""}>{p} ({esc(models[p])})</option>' for p in PROVIDERS)
        rows += (f'<tr><td><b>{st}</b><div class="mut" style="font-size:12.5px">{esc(STAGE_HELP[st])}</div></td>'
                 f'<td><select name="{st}">{opts}</select></td></tr>')
    warn = ""
    if "anthropic" in {stages.get(s, default) for s in STAGES} and not os.environ.get("ANTHROPIC_API_KEY"):
        warn = ("<div class='banner bad'>A stage uses anthropic but ANTHROPIC_API_KEY is not set in the environment "
                "this server was started from. Those stages will fail until you export it and restart.</div>")
    body = ("<h1>Models</h1><p class='sub'>Which model handles each stage. Applies to the next action, no restart needed. "
            "Anthropic stages send the posting and your facts to the Anthropic API; Ollama stays on this machine.</p>"
            f"{warn}<form method='post' action='/settings/save'>{hidden(tok)}<div class='card'><table>{rows}</table></div>"
            "<p><button class='pri'>Save</button> <span class='mut'>Only the <code>llm.stages</code> block of settings.yaml is changed; "
            "the previous file is kept as <code>settings.yaml.bak</code>.</span></p></form>"
            f"<p class='mut'>Current: {esc(_models(cfg))}</p>")
    return "Models", body


def do_settings_save(ctx, f):
    from .llm import STAGES
    try:
        actions.save_stages(ctx["cfg"], {s: f.get(s, "") for s in STAGES})
    except actions.ActionError as e:
        raise Redirect(link("/settings", err=str(e)))
    raise Redirect(link("/settings", ok="Saved. The next action uses these models."))


# --- facts from a CV -----------------------------------------------------------------------

FACTS_ORDER = ("identity", "experience_total", "role", "education", "certification", "language", "skill")
FACTS_TITLE = {"identity": "Contact details", "experience_total": "Years of experience", "role": "Jobs",
               "education": "Education", "certification": "Certifications", "language": "Languages", "skill": "Skills"}
FACTS_LABEL = {"field": "Field", "value": "Value", "language": "Language", "level": "Level", "employer": "Employer",
               "title": "Job title", "dates": "Dates", "stack": "Technologies (comma separated)", "work": "What you did",
               "highlights": "Achievements (one per line)", "category": "Category", "item": "Skill", "name": "Name",
               "issuer": "Issuer", "year": "Year", "institution": "Institution", "degree": "Degree as written"}


def _p(pid: str, field: str) -> str:
    return f"{pid}__{field}"


def _fact_summary(p: dict) -> str:
    f = p["fields"]
    main = {"identity": f"{f.get('field')}: {f.get('value')}", "role": f"{f.get('title')} at {f.get('employer')} ({f.get('dates')})",
            "skill": f.get("item"), "certification": f.get("name"), "education": f"{f.get('institution')} {f.get('field')}",
            "language": f"{f.get('language')}: {f.get('level')}", "experience_total": f.get("value")}[p["kind"]]
    return esc(str(main))


def _fact_row(p: dict) -> str:
    pid, f, kind = p["id"], p["fields"], p["kind"]
    quote = f"<div class='mut' style='font-size:12.5px;margin-top:4px'>From your CV: “{esc(p['quote'])}”</div>"
    if kind == "skill":
        lv = "".join(f"<option value='{v}'>{v}</option>" for v in factsimport.LEVELS)
        cats = "".join(f"<option value='{c}'{' selected' if c == f['category'] else ''}>{c}</option>" for c in factsimport.SKILL_CATEGORIES)
        return (f"<div class='card'><div class='row'><b>{esc(f['item'])}</b>"
                f"<input type='hidden' name='{_p(pid, 'item')}' value='{esc(f['item'])}'>"
                f"<select name='{_p(pid, 'category')}'>{cats}</select>"
                f"<select name='{_p(pid, 'level')}' aria-label='level'><option value=''>— leave out —</option>{lv}</select></div>{quote}</div>")
    inputs = ""
    for name in factsimport.KINDS[kind][0]:
        val = f.get(name, "")
        label = FACTS_LABEL.get(name, name)
        if name == "field" and kind == "identity":
            inputs += f"<input type='hidden' name='{_p(pid, name)}' value='{esc(val)}'>"
            continue
        if name == "highlights":
            inputs += f"<label>{label}</label><textarea name='{_p(pid, name)}' rows='3'>{esc(chr(10).join(val))}</textarea>"
        elif name == "work":
            inputs += f"<label>{label}</label><textarea name='{_p(pid, name)}' rows='2'>{esc(val)}</textarea>"
        elif name == "stack":
            inputs += f"<label>{label}</label><input type='text' name='{_p(pid, name)}' value='{esc(', '.join(val))}'>"
        else:
            inputs += f"<label>{label}</label><input type='text' name='{_p(pid, name)}' value='{esc(val)}'>"
    head = f"<label style='display:flex;gap:8px;align-items:center'><input type='checkbox' name='{_p(pid, 'keep')}' value='1' checked> <b>Keep</b> <span class='mut'>{_fact_summary(p)}</span></label>"
    return f"<div class='card'>{head}{inputs}{quote}</div>"


def page_facts(ctx, q):
    cfg, tok = ctx["cfg"], ctx["tok"]
    llm = cfg.settings.get("llm", {})
    prov = (llm.get("stages") or {}).get("extract") or llm.get("provider", "ollama")
    model = (llm.get(prov) or {}).get("model", "?")
    job, state = factsimport.CURRENT, factsimport.load_state(cfg)
    template = factsimport.is_template(cfg.facts)
    where = ("Your CV text is sent to the Anthropic API to be read." if prov == "anthropic"
             else "Your CV is read by Ollama on this computer and goes nowhere else.")
    warn = runner.anthropic_key_problem(cfg, {"extract"})
    body = ("<h1>Facts</h1><p class='sub'>Build your <code>facts.yaml</code> from a CV (PDF, DOCX or text; LinkedIn's "
            "<i>Save to PDF</i> works too). The model only proposes; nothing is written until you review it and press Save.</p>")
    if warn:
        body += f"<div class='banner bad'>{esc(warn)}</div>"
    if template:
        body += ("<div class='banner'>Your facts.yaml is still the fictional example (Alex Example). Saving an import "
                 "replaces it with your own facts; the example is kept as <code>facts.yaml.bak</code>.</div>")
    running = factsimport.is_running()
    if job and job.state == "running":
        body += f"<!--refresh--><div class='card'><span class='badge'>Working</span> {esc(job.message)}</div>"
    elif job and job.state == "failed":
        body += f"<div class='banner bad'>Could not read the CV: {esc(job.message)}</div>"
    body += ("<div class='card'><form method='post' action='/facts/upload' enctype='multipart/form-data' data-busy>"
             f"{hidden(tok)}<label>Your CV</label><input type='file' name='cv' accept='.pdf,.docx,.txt,.md'>"
             f"<p class='mut'>Read by <b>{esc(prov)}</b> ({esc(model)}). {where} Change this on the Models page (stage <code>extract</code>).</p>"
             "<p class='mut'>Your CV is also saved in your <code>cvs/</code> folder (a file with the same name is replaced) "
             "and listed in <code>cvs.yaml</code>.</p>"
             f"<button class='pri'{' disabled' if running else ''}>Read my CV</button> <span class='busy mut' hidden>Uploading…</span></form></div>")
    if state:
        props = state.get("proposals") or []
        new = [p for p in props if not p.get("existing")]
        old = [p for p in props if p.get("existing")]
        body += f"<h2>Review: {esc(state.get('filename', 'CV'))}</h2>"
        if state.get("dropped"):
            body += (f"<p class='mut'>{int(state['dropped'])} suggestion(s) were discarded because their source line was not in your CV.</p>")
        if not new:
            body += "<p>Nothing new to add: everything found is already in your facts.</p>"
        else:
            body += ("<p class='mut'>Edit anything that is wrong and untick what you don't want. For each skill pick how you know it: "
                     "<b>production</b> (used in professional systems), <b>project</b> (independent projects only), <b>working</b> "
                     "(can work with it) or <b>familiarity</b> (concepts only). A skill left on “leave out” is not saved, and unsaved "
                     "skills are never used in letters. Degree completion and availability stay <code>TODO</code> for you to fill in.</p>")
            rows = ""
            for kind in FACTS_ORDER:
                group = [p for p in new if p["kind"] == kind]
                if group:
                    rows += f"<h3>{FACTS_TITLE[kind]} ({len(group)})</h3>" + "".join(_fact_row(p) for p in group)
            body += (f"<form method='post' action='/facts/save'>{hidden(tok)}{rows}"
                     "<p><button class='pri'>Save to facts.yaml</button> <span class='mut'>A backup is kept as facts.yaml.bak.</span></p></form>")
        if old:
            body += ("<details><summary>Already in your facts (" + str(len(old)) + ")</summary>"
                     + "".join(f"<div class='mut'>· {_fact_summary(p)}</div>" for p in old) + "</details>")
        body += (f"<form method='post' action='/facts/discard' style='margin-top:14px'>{hidden(tok)}"
                 "<button class='danger'>Discard this import</button></form>")
    return "Facts", body


def do_facts_upload(ctx, f):
    cfg = ctx["cfg"]
    problem = runner.anthropic_key_problem(cfg, {"extract"})
    if problem:
        raise Redirect(link("/facts", err=problem))
    name, data = (f.get("_files") or {}).get("cv", ("", b""))
    if not name or not data:
        raise Redirect(link("/facts", err="Choose a file first."))
    try:
        text = factsimport.read_document(name, data)
        stored = cvstore.store_cv(cfg.root, name, data)          # into cvs/ and cvs.yaml, replacing a same-named file
        factsimport.start(cfg.root, make_llm, name, text)
    except factsimport.ImportProblem as e:
        raise Redirect(link("/facts", err=str(e)))
    raise Redirect(link("/facts", ok=_stored_note(stored, False) + " Reading your CV. This page refreshes while it works."))


def do_facts_save(ctx, f):
    cfg = ctx["cfg"]
    state = factsimport.load_state(cfg)
    if not state:
        raise Redirect(link("/facts", err="There is no pending import."))
    items = []
    for p in state["proposals"]:
        if p.get("existing"):
            continue
        pid, kind = p["id"], p["kind"]
        get = lambda name: f.get(_p(pid, name), "").strip()   # noqa: E731
        fields = {}
        for name in factsimport.KINDS[kind][0]:
            if name in factsimport.LIST_FIELDS:
                fields[name] = factsimport._clean_list(get(name) if name == "stack" else
                                                       [x.strip() for x in get(name).splitlines()])
            else:
                fields[name] = factsimport._clean_str(get(name))
        if kind == "skill":
            level = get("level")
            if level not in factsimport.LEVELS:
                continue            # no level chosen: not saved
            fields["level"] = level
            if fields["category"] not in factsimport.SKILL_CATEGORIES:
                fields["category"] = "other"
        elif not f.get(_p(pid, "keep")):
            continue
        if kind == "identity" and fields["field"] not in factsimport.IDENTITY_FIELDS:
            continue
        if not fields[factsimport.KINDS[kind][1]]:
            continue
        items.append({"kind": kind, "fields": fields, "quote": p.get("quote", "")})
    if not items:
        raise Redirect(link("/facts", err="Nothing is selected to save."))
    try:
        factsimport.apply_import(cfg, items, from_skeleton=factsimport.is_template(cfg.facts))
    except factsimport.ImportProblem as e:
        raise Redirect(link("/facts", err=str(e)))
    factsimport.discard_state(cfg)
    raise Redirect(link("/facts", ok=f"Saved {len(items)} items to {cfg.root / 'config' / 'facts.yaml'}. Open it to resolve the "
                                    "remaining TODO lines (`applybot doctor` lists them)."))


def do_facts_discard(ctx, f):
    factsimport.discard_state(ctx["cfg"])
    raise Redirect(link("/facts", ok="Import discarded."))


# --- profile from a CV ------------------------------------------------------------------------

TIER_LABEL = (("", "— leave out —"), ("primary", "primary"), ("secondary", "secondary"),
              ("selective", "always review"))


def _checks(name: str, options, chosen) -> str:
    return " ".join(f"<label style='display:inline-flex;gap:4px;align-items:center;margin-right:12px'>"
                    f"<input type='checkbox' name='{name}' value='{esc(o)}'{' checked' if o in chosen else ''}> {esc(o)}</label>"
                    for o in options)


def _first(props: list, kind: str, field: str = "") -> dict | None:
    return next((p for p in props if p["kind"] == kind and (not field or p["field"] == field)), None)


def _src(p: dict | None) -> str:
    return f"<div class='mut' style='font-size:12.5px'>From your text: “{esc(p['quote'])}”</div>" if p else ""


def page_profile(ctx, q):
    cfg, tok = ctx["cfg"], ctx["tok"]
    llm = cfg.settings.get("llm", {})
    prov = (llm.get("stages") or {}).get("extract") or llm.get("provider", "ollama")
    model = (llm.get(prov) or {}).get("model", "?")
    job, state = profileimport.CURRENT, profileimport.load_state(cfg)
    starter = profileimport.is_starter_profile(cfg.profile)
    where = ("Your text is sent to the Anthropic API to be read." if prov == "anthropic"
             else "Your text is read by Ollama on this computer and goes nowhere else.")
    warn = runner.anthropic_key_problem(cfg, {"extract"})
    body = ("<h1>Profile</h1><p class='sub'>Build your <code>profile.yaml</code>, your CV list and your job-title keywords from a CV "
            "and, if you like, a cover letter. The model only proposes; nothing is written until you review it and press Save.</p>")
    if warn:
        body += f"<div class='banner bad'>{esc(warn)}</div>"
    body += ("<div class='banner'>Your profile.yaml is still the fictional example (Alex Example). Saving replaces it; the example "
             "is kept as <code>profile.yaml.bak</code>.</div>" if starter else
             "<div class='banner'>You already have a profile. Saving writes <code>config/profile.suggested.yaml</code> next to it "
             "and does not change your <code>profile.yaml</code>.</div>")
    running = profileimport.is_running()
    if job and job.state == "running":
        body += f"<!--refresh--><div class='card'><span class='badge'>Working</span> {esc(job.message)}</div>"
    elif job and job.state == "failed":
        body += f"<div class='banner bad'>Could not read the text: {esc(job.message)}</div>"
    body += ("<div class='card'><form method='post' action='/profile/upload' enctype='multipart/form-data' data-busy>"
             f"{hidden(tok)}<label>Your CV</label><input type='file' name='cv' accept='.pdf,.docx,.txt,.md'>"
             "<label>A cover letter or profile text (optional)</label><input type='file' name='extra' accept='.pdf,.docx,.txt,.md'>"
             "<p class='mut'>The optional document is used only for your positioning, target roles and preferences, "
             "never as proof of what you did. A letter written for one job leans towards that job, so check what it suggests.</p>"
             "<p class='mut'>Both files are also saved in your <code>cvs/</code> folder (a file with the same name is replaced) "
             "and your CV is listed in <code>cvs.yaml</code>.</p>"
             f"<p class='mut'>Read by <b>{esc(prov)}</b> ({esc(model)}). {where} Change this on the Models page (stage <code>extract</code>).</p>"
             f"<button class='pri'{' disabled' if running else ''}>Read my documents</button> <span class='busy mut' hidden>Uploading…</span></form></div>")
    if state:
        body += _profile_review(cfg, tok, state)
    return "Profile", body


def _profile_review(cfg, tok: str, state: dict) -> str:
    props = state.get("proposals") or []
    hint = profileimport.facts_hint(cfg.facts)
    one = lambda kind, field="": (_first(props, kind, field) or {}).get("value", "")   # noqa: E731
    out = f"<h2>Review: {esc(state.get('filename', 'CV'))}" + (f" + {esc(state['extra'])}" if state.get("extra") else "") + "</h2>"
    if state.get("dropped"):
        out += f"<p class='mut'>{int(state['dropped'])} suggestion(s) were discarded because their source line was not in your text.</p>"
    text = lambda name, label, val, ph="": (f"<label>{label}</label><input type='text' name='f__{name}' value='{esc(val)}'"   # noqa: E731
                                            f"{(' placeholder=' + chr(39) + esc(ph) + chr(39)) if ph else ''}>")
    f = f"<form method='post' action='/profile/save'>{hidden(tok)}"
    f += "<h3>About you</h3><div class='card'>"
    f += text("name", "Name", one("name") or hint.get("name", "")) + _src(_first(props, "name"))
    f += text("headline", "Headline", one("headline")) + _src(_first(props, "headline"))
    f += text("seniority", "Seniority", one("seniority")) + _src(_first(props, "seniority"))
    f += text("experience_years", "Years of experience", one("experience_years")) + _src(_first(props, "experience_years"))
    f += text("home_base", "Based in", one("home_base") or hint.get("location", "")) + _src(_first(props, "home_base"))
    langs = [p["value"] for p in props if p["kind"] == "language"] or hint.get("languages", [])
    f += text("languages", "Languages (comma separated)", ", ".join(langs))
    f += "</div><h3>Where you can work</h3><div class='card'>"
    f += ("<p class='mut'>This is never guessed. It decides which postings are rejected because of where they are. "
          "A posting limited to somewhere you are not authorised goes to review or is rejected.</p>")
    f += text("home_country", "Home country", profileimport.guess_country(one("home_base") or hint.get("location", "")),
              "the country only, for example Portugal")
    f += "<label>Also authorised to work in</label>" + _checks("f__regions", profileimport.REGIONS, [])
    f += "<p><label style='display:flex;gap:6px;align-items:center'><input type='checkbox' name='f__relocation' value='1'> I would relocate</label></p>"
    reloc = _first(props, "preference", "relocation")
    f += _src(reloc)
    f += "</div><h3>What you want</h3><div class='card'>"
    modes = [m for m in profileimport.MODES if m in one("preference", "working_mode").lower()] or ["remote", "hybrid"]
    types = [t for t in profileimport.TYPES if t in one("preference", "employment_types").lower()] or ["permanent", "contract"]
    f += "<label>Working mode</label>" + _checks("f__mode", profileimport.MODES, modes) + _src(_first(props, "preference", "working_mode"))
    f += "<label>Employment types</label>" + _checks("f__type", profileimport.TYPES, types) + _src(_first(props, "preference", "employment_types"))
    f += text("availability", "Availability", one("preference", "availability"), "for example Immediately") + _src(_first(props, "preference", "availability"))
    f += text("geography", "Where you look", "", "for example Remote within Europe")
    f += "</div><h3>Job families</h3>"
    f += ("<p class='mut'>The titles you want to be matched with. <b>primary</b> and <b>secondary</b> are matched normally; "
          "<b>always review</b> sends every match to you, with the reason you give. Titles your text itself uses start as primary; "
          "the rest start left out. Every family you keep is given your CV.</p>")
    for p in (x for x in props if x["kind"] == "family"):
        sel = "".join(f"<option value='{v}'{' selected' if v == p.get('tier', '') else ''}>{lab}</option>" for v, lab in TIER_LABEL)
        f += (f"<div class='card'><div class='row'><input type='text' name='{_p(p['id'], 'value')}' value='{esc(p['value'])}'>"
              f"<select name='{_p(p['id'], 'tier')}'>{sel}</select>"
              f"<input type='text' name='{_p(p['id'], 'reason')}' placeholder='why it is always reviewed'></div>"
              f"<div class='mut' style='font-size:12.5px'>From your text: “{esc(p['quote'])}”</div></div>")
    f += "<h3>How you are described</h3><div class='card'>"
    pos = _first(props, "positioning")
    f += (f"<label>Positioning</label><textarea name='f__positioning' rows='4'>{esc(pos['value'] if pos else '')}</textarea>" + _src(pos))
    f += "<label>Capabilities (untick what you don't want)</label>"
    for p in (x for x in props if x["kind"] == "capability"):
        f += (f"<div class='row'><input type='checkbox' name='{_p(p['id'], 'keep')}' value='1' checked>"
              f"<input type='text' name='{_p(p['id'], 'value')}' value='{esc(p['value'])}'></div>")
    kws = list(state.get("keywords") or [])
    f += "</div><h3>Job-title keywords</h3><div class='card'>"
    if profileimport.criteria_is_starter(cfg):
        skip = profileimport.starter_excludes(cfg)
        clash = profileimport.conflicts(kws, skip)
        f += ("<p class='mut'>A cheap filter before any model call. Your <code>criteria.yaml</code> still has the starter lists, "
              "written for a backend-engineering search, so saving <b>replaces</b> them with the two lists below. "
              "A posting is skipped if its title has none of the keywords, or has a skip word. One per line.</p>"
              f"<label>Keywords a title must contain one of</label><textarea name='f__keywords' rows='6'>{esc(chr(10).join(kws))}</textarea>"
              f"<label>Skip a posting whose title contains</label><textarea name='f__exclude' rows='5'>"
              f"{esc(chr(10).join(e for e in skip if e not in clash))}</textarea>")
        if clash:
            f += ("<p class='mut'>Left out of the skip list because they match a keyword you want: "
                  + esc(", ".join(clash)) + ".</p>")
    else:
        f += ("<p class='mut'>A cheap filter before any model call: a posting whose title contains none of the keywords is skipped. "
              "These are <b>added</b> to your criteria.yaml list; nothing there is removed. One per line.</p>"
              f"<textarea name='f__keywords' rows='6'>{esc(chr(10).join(kws))}</textarea>")
    f += "</div>"
    f += ("<p><button class='pri'>Save profile</button> <span class='mut'>Your CV list and keywords are updated too; "
          "a backup of any file replaced is kept as .bak.</span></p></form>")
    f += (f"<form method='post' action='/profile/discard' style='margin-top:14px'>{hidden(tok)}"
          "<button class='danger'>Discard this import</button></form>")
    return out + f


def do_profile_upload(ctx, f):
    cfg = ctx["cfg"]
    problem = runner.anthropic_key_problem(cfg, {"extract"})
    if problem:
        raise Redirect(link("/profile", err=problem))
    files = f.get("_files") or {}
    name, data = files.get("cv", ("", b""))
    if not name or not data:
        raise Redirect(link("/profile", err="Choose your CV first."))
    xname, xdata = files.get("extra", ("", b""))
    try:
        text = factsimport.read_document(name, data)
        extra = factsimport.read_document(xname, xdata) if xname and xdata else ""
        stored = cvstore.store_cv(cfg.root, name, data)          # into cvs/ and cvs.yaml, replacing a same-named file
        if extra:
            cvstore.store_letter(cfg.root, xname, xdata)         # kept in cvs/cover-letters/ for later
        profileimport.start(cfg.root, make_llm, name, profileimport.combine(text, extra), xname if extra else "",
                            stored["file"])
    except factsimport.ImportProblem as e:
        raise Redirect(link("/profile", err=str(e)))
    raise Redirect(link("/profile", ok=_stored_note(stored, bool(extra)) + " Reading your documents. This page refreshes while it works."))


def _stored_note(stored: dict, letter: bool) -> str:
    note = (f"Your CV replaced cvs/{stored['file']}." if stored["replaced"] else f"Your CV was saved as cvs/{stored['file']}.")
    note += " It is listed in cvs.yaml." if stored["added"] else ""
    return note + (" Your cover letter was kept in cvs/cover-letters/." if letter else "")


def do_profile_save(ctx, f):
    cfg = ctx["cfg"]
    state = profileimport.load_state(cfg)
    if not state:
        raise Redirect(link("/profile", err="There is no pending import."))
    multi = f.get("_multi") or {}
    got = lambda name: f.get(f"f__{name}", "").strip()   # noqa: E731
    fams: dict = {"primary": [], "secondary": [], "selective": {}}
    caps = []
    for p in state["proposals"]:
        val = f.get(_p(p["id"], "value"), "").strip()
        if p["kind"] == "family":
            tier, title = f.get(_p(p["id"], "tier"), ""), factsimport._clean_str(val)
            if tier == "selective" and title:
                fams["selective"][title] = f.get(_p(p["id"], "reason"), "").strip()
            elif tier in ("primary", "secondary") and title and title not in fams["primary"] + fams["secondary"]:
                fams[tier].append(title)
        elif p["kind"] == "capability" and f.get(_p(p["id"], "keep")) and val:
            caps.append(val)
    answers = {
        "name": got("name"), "headline": got("headline"), "seniority": got("seniority"),
        "experience_years": got("experience_years"), "home_base": got("home_base"),
        "home_country": got("home_country"), "authorised_regions": multi.get("f__regions", []),
        "accepts_relocation": bool(f.get("f__relocation")), "languages": factsimport._clean_list(got("languages")),
        "positioning": got("positioning"), "families": fams, "capabilities": caps,
        "working_mode": multi.get("f__mode", []), "employment_types": multi.get("f__type", []),
        "availability": got("availability"), "geography": got("geography"),
    }
    try:
        profile = profileimport.build_profile(answers)
        starter = profileimport.is_starter_profile(cfg.profile)
        target = profileimport.apply_profile(cfg, profile)
        families = fams["primary"] + fams["secondary"] + list(fams["selective"])
        cvs = (cvstore.set_families(cfg.root, state["cv_file"], families, exclusive=starter)
               if state.get("cv_file") else None)
        crit = profileimport.apply_criteria(cfg, got("keywords"), f.get("f__exclude"))
    except factsimport.ImportProblem as e:
        raise Redirect(link("/profile", err=str(e)))
    profileimport.discard_state(cfg)
    msg = f"Saved your profile to {target}."
    if cvs:
        if cvs["added"]:
            msg += f" {len(cvs['added'])} job famil{'y' if len(cvs['added']) == 1 else 'ies'} now use your CV in cvs.yaml."
        if cvs["elsewhere"]:
            msg += (" Already used by another CV, so left there: "
                    + ", ".join(f"{f} ({c})" for f, c in cvs["elsewhere"].items()) + ".")
    if crit["mode"] == "replaced":
        msg += f" The starter title keywords in criteria.yaml were replaced by your {crit['added']}."
    elif crit["added"]:
        msg += f" Added {crit['added']} title keyword(s) to criteria.yaml."
    if crit["removed"]:
        msg += " Removed from the skip list because they match a keyword you want: " + ", ".join(crit["removed"]) + "."
    if crit["conflicts"]:
        msg += (" Careful: your skip list in criteria.yaml still contains " + ", ".join(crit["conflicts"])
                + ", which would skip postings you want. Edit it if that is not what you mean.")
    raise Redirect(link("/profile", ok=msg + " Open the file to resolve any TODO lines (`applybot doctor` lists them)."))


def do_profile_discard(ctx, f):
    profileimport.discard_state(ctx["cfg"])
    raise Redirect(link("/profile", ok="Import discarded."))


def parse_multipart(ctype: str, raw: bytes) -> dict:
    """A multipart/form-data body -> {field: text, '_files': {field: (filename, bytes)}}."""
    from email import policy
    from email.parser import BytesParser
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
    f: dict = {}
    files: dict = {}
    if msg.is_multipart():
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            data = part.get_payload(decode=True) or b""
            if part.get_filename() is not None:
                files[name] = (part.get_filename(), data)
            else:
                f[name] = data.decode("utf-8", "replace")
    f["_files"] = files
    f["_multi"] = {k: [v] for k, v in f.items() if isinstance(v, str)}
    return f


# --- sources and runs ----------------------------------------------------------------------

SRC_HELP = {"greenhouse": "boards.greenhouse.io/&lt;token&gt;  or  job-boards.greenhouse.io/&lt;token&gt;",
            "lever": "jobs.lever.co/&lt;token&gt;", "ashby": "jobs.ashbyhq.com/&lt;token&gt;"}


def page_sources(ctx, q):
    cfg, tok = ctx["cfg"], ctx["tok"]
    s = runner.read_sources(cfg)
    boxes = "".join(
        f"<div class='card'><label><b>{k.capitalize()}</b> <span class='mut'>· {SRC_HELP[k]}</span></label>"
        f"<textarea name='{k}' rows='5' placeholder='one token per line'>{esc(chr(10).join(s[k]))}</textarea></div>"
        for k in runner.KINDS)
    cats = s["remotive"]["categories"]
    checks = "".join(
        f"<label style='display:inline-flex;gap:6px;align-items:center;margin-right:14px'>"
        f"<input type='checkbox' name='cat' value='{c}'{' checked' if c in cats else ''}> {c}</label>"
        for c in runner.REMOTIVE_KNOWN)
    extra = ", ".join(c for c in cats if c not in runner.REMOTIVE_KNOWN)
    kinds = "".join(f"<option>{k}</option>" for k in (*runner.KINDS, "remotive"))
    body = (
        "<h1>Sources</h1><p class='sub'>Where postings come from. Only public job-board APIs; no scraping and no logins. "
        "Which titles are wanted is decided afterwards by <code>config/criteria.yaml</code>.</p>"
        f"<form method='post' action='/sources/save'>{hidden(tok)}{boxes}"
        "<div class='card'><label style='display:flex;gap:8px;align-items:center'><input type='checkbox' name='remotive' value='1'"
        f"{' checked' if s['remotive']['enabled'] else ''}> <b>Remotive</b> <span class='mut'>· remote jobs, by category</span></label>"
        f"<p>{checks}</p><label>Other category slugs <span class='mut'>(comma separated; Check them below first)</span></label>"
        f"<input type='text' name='cat_extra' value='{esc(extra)}'>"
        "<p class='mut'>A category only decides which feed is downloaded. Whether a title is wanted (Software Architect, "
        "Backend Engineer, Scrum Master…) is decided afterwards by the keywords in <code>criteria.yaml</code>. "
        "Confirmed to work: software-dev, devops. The others are Remotive's category names but not verified here: Check before relying on them.</p></div>"
        "<p><button class='pri'>Save sources</button> <span class='mut'>The previous file is kept as sources.yaml.bak.</span></p></form>"
        "<h2>Check a token</h2><div class='card'><p class='mut' style='margin-top:0'>Asks the board's public API whether the token exists "
        "and how many postings it lists. Nothing is saved.</p>"
        f"<form method='post' action='/sources/check' data-busy>{hidden(tok)}<div class='row'>"
        f"<select name='kind'>{kinds}</select><input type='text' name='name' placeholder='token or category' style='max-width:260px'>"
        "<button>Check</button> <span class='busy mut' hidden>Checking…</span></div></form></div>")
    return "Sources", body


def do_sources_save(ctx, f):
    cats = list(f.get("_multi", {}).get("cat", []))
    try:
        runner.save_sources(ctx["cfg"], {
            **{k: runner.parse_tokens(f.get(k, "")) for k in runner.KINDS},
            "remotive": {"enabled": bool(f.get("remotive")),
                         "categories": cats + runner.parse_tokens(f.get("cat_extra", ""))}})
    except runner.SourceError as e:
        raise Redirect(link("/sources", err=str(e)))
    raise Redirect(link("/sources", ok="Saved. The next run uses these sources."))


def do_sources_check(ctx, f):
    ok, msg = runner.check_source(f.get("kind", ""), f.get("name", ""), make_http())
    raise Redirect(link("/sources", **({"ok": msg} if ok else {"err": msg})))


def _fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def page_run(ctx, q):
    cfg, conn, tok = ctx["cfg"], ctx["conn"], ctx["tok"]
    run = runner.CURRENT
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='new'").fetchone()[0]
    draftable = conn.execute("""SELECT COUNT(*) FROM scores s LEFT JOIN applications a ON a.job_id=s.job_id
                                WHERE s.decision IN ('human','auto_eligible') AND a.job_id IS NULL""").fetchone()[0]
    good = (cfg.criteria.get("thresholds") or {}).get("good", 70)
    status = ""
    if run:
        pct = int(100 * run.done / run.total) if run.total else 0
        elapsed = _fmt_secs((run.finished or time.time()) - run.started)
        head = {"running": f"Running: {esc(run.stage)}", "done": "Finished", "stopped": "Stopped", "failed": "Failed"}[run.state]
        cls = {"done": "ok", "stopped": "ok", "failed": "bad"}.get(run.state, "")
        bar = f"<div class='bar'><i style='width:{pct}%'></i></div>" if run.state == "running" and run.total else ""
        prog = f" · {run.done}/{run.total}" if run.state == "running" and run.total else ""
        cur = f"<div>{esc(run.current)}</div>" if run.current else ""
        stop = (f"<form method='post' action='/run/cancel'>{hidden(tok)}<button class='danger'>Stop after the current job</button></form>"
                if run.state == "running" else "")
        status = (f"{'<!--refresh-->' if run.state == 'running' else ''}<h2>Status</h2><div class='card'>"
                  f"<span class='badge {cls}'>{head}</span> <span class='mut'>{elapsed}{prog}</span>{cur}{bar}"
                  f"<pre style='max-height:220px;overflow:auto'>{esc(chr(10).join(run.log[-40:]))}</pre>{stop}</div>")
    running = runner.is_running()
    dis = " disabled" if running else ""
    warn = runner.anthropic_key_problem(cfg, {"score", "draft"})
    body = (
        "<h1>Run</h1><p class='sub'>The same steps as <code>applybot run</code>: fetch postings, score them, draft letters. "
        "Nothing is approved or sent from here; scored postings are listed under <a href='/postings'>Postings</a> and drafts land in Review.</p>"
        + (f"<div class='banner bad'>{esc(warn)}</div>" if warn else "")
        + f"<div class='card mut'>Waiting to be scored: <b>{pending}</b> · scored, no letter yet: <b>{draftable}</b> · "
          f"Models: {esc(_models(cfg))}</div>"
        f"<form method='post' action='/run/start'>{hidden(tok)}<div class='card'>"
        "<label><input type='checkbox' name='ingest' value='1' checked> <b>1. Fetch</b> new postings from your sources</label>"
        "<label><input type='checkbox' name='score' value='1' checked> <b>2. Score</b> at most "
        "<input type='number' name='score_limit' value='10' min='1' max='500' style='width:80px'> postings "
        "<select name='order'><option value='relevance'>best matches first</option><option value='oldest'>oldest first</option></select> "
        "<span class='mut'>(a local model needs about a minute and a half each)</span></label>"
        "<label><input type='checkbox' name='draft' value='1' checked> <b>3. Draft</b> letters for at most "
        "<input type='number' name='draft_limit' value='5' min='1' max='200' style='width:80px'> jobs scoring at least "
        f"<input type='number' name='min_score' value='{good}' min='0' max='100' style='width:80px'> "
        "<span class='mut'>(best scores first)</span></label>"
        f"<p><button class='pri'{dis}>{'A run is in progress' if running else 'Start'}</button></p></div></form>"
        + status)
    return "Run", body


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def do_run_start(ctx, f):
    cfg = ctx["cfg"]
    steps = [s for s in ("ingest", "score", "draft") if f.get(s)]
    if not steps:
        raise Redirect(link("/run", err="Pick at least one step."))
    problem = runner.anthropic_key_problem(cfg, steps)
    if problem:
        raise Redirect(link("/run", err=problem))
    opts = {"score_limit": _int(f.get("score_limit"), 10, 1, 500), "draft_limit": _int(f.get("draft_limit"), 5, 1, 200),
            "min_score": _int(f.get("min_score"), 70, 0, 100),
            "order": "oldest" if f.get("order") == "oldest" else "relevance"}
    try:
        runner.start(cfg.root, make_llm, steps, opts)
    except runner.SourceError as e:
        raise Redirect(link("/run", err=str(e)))
    raise Redirect(link("/run", ok="Started. This page refreshes while it runs; you can leave it."))


def do_run_cancel(ctx, f):
    if runner.CURRENT and runner.is_running():
        runner.CURRENT.stop.set()
        runner.CURRENT.say("Stop requested; finishing the current job…")
    raise Redirect(link("/run"))


KIND_LABEL = {"draft": "first draft", "revision": "automatic revision", "edit": "edited by you", "sent": "sent to the recruiter"}


POSTING_TABS = {
    "scored": ("Scored", "Fetched postings the model has scored, best first. Open one to see why, and the letter if there is one."),
    "waiting": ("Waiting to be scored", "Passed the title filter, not scored yet. Run the Score step on the Run page."),
    "filtered": ("Filtered out", "Dropped by the title filter before any model was used, with the reason."),
}


def page_postings(ctx, q):
    conn = ctx["conn"]
    tab = q.get("tab", "scored")
    if tab not in POSTING_TABS:
        tab = "scored"
    name, blurb = POSTING_TABS[tab]
    counts = {r["k"]: r["c"] for r in conn.execute("SELECT status k, COUNT(*) c FROM jobs GROUP BY status")}
    n = {"scored": counts.get("scored", 0), "waiting": counts.get("new", 0), "filtered": counts.get("filtered", 0)}
    tabs = "".join(f'<a class="{"on" if t == tab else ""}" href="{link("/postings", tab=t)}">{POSTING_TABS[t][0]} ({n[t]})</a>'
                   for t in POSTING_TABS)
    cap = 200
    if tab == "scored":
        rows = conn.execute(
            """SELECT j.id, j.company, j.title, j.location, s.overall, s.decision, s.family, s.cv_id, a.status AS app
               FROM jobs j JOIN scores s ON s.job_id=j.id LEFT JOIN applications a ON a.job_id=j.id
               WHERE j.status='scored' ORDER BY s.overall DESC, j.fetched_at DESC LIMIT ?""", (cap,)).fetchall()
        trs = "".join(
            f'<tr><td class="n">{r["overall"] if r["overall"] is not None else "-"}</td><td>{badge(r["decision"])}</td>'
            f'<td>{esc(r["company"] or "")}</td><td><a href="{link("/job", id=r["id"])}">{esc(r["title"] or "")}</a></td>'
            f'<td class="mut">{esc(r["location"] or "")}</td><td class="mut">{esc(r["family"] or "-")}</td>'
            f'<td>{badge(r["app"]) if r["app"] else "<span class=mut>no letter yet</span>"}</td></tr>' for r in rows)
        head = "<tr><th>Score</th><th>Decision</th><th>Company</th><th>Role</th><th>Location</th><th>Family</th><th>Letter</th></tr>"
    else:
        status = "new" if tab == "waiting" else "filtered"
        rows = conn.execute(
            """SELECT id, company, title, location, filter_reason FROM jobs WHERE status=?
               ORDER BY fetched_at DESC LIMIT ?""", (status, cap)).fetchall()
        extra = tab == "filtered"
        trs = "".join(
            f'<tr><td>{esc(r["company"] or "")}</td><td><a href="{link("/job", id=r["id"])}">{esc(r["title"] or "")}</a></td>'
            f'<td class="mut">{esc(r["location"] or "")}</td>' + (f'<td class="mut">{esc(r["filter_reason"] or "")}</td>' if extra else "") + "</tr>"
            for r in rows)
        head = "<tr><th>Company</th><th>Role</th><th>Location</th>" + ("<th>Why</th>" if extra else "") + "</tr>"
    table = (f"<table>{head}{trs}</table>" if rows else "<div class='card mut'>Nothing here.</div>")
    more = f"<p class='mut'>Showing the first {cap} of {n[tab]}.</p>" if n[tab] > cap else ""
    return "Postings", f"<h1>Postings</h1><p class='sub'>{esc(blurb)}</p><div class='tabs'>{tabs}</div>{table}{more}"


def page_history(ctx, q):
    conn = ctx["conn"]
    jid = q.get("job")
    if not jid:
        rows = history.postings(conn)
        trs = "".join(
            f'<tr><td><a href="{link("/history", job=r["job_id"])}">{esc(r["title"] or r["job_id"])}</a></td>'
            f'<td>{esc(r["company"] or "")}</td><td class="mut">{r["n"]}</td><td class="mut">{esc((r["last_at"] or "")[:16].replace("T", " "))}</td>'
            f'<td>{badge(r["status"]) if r["status"] else "<span class=mut>not saved</span>"}</td></tr>' for r in rows)
        table = (f"<table><tr><th>Posting</th><th>Company</th><th>Versions</th><th>Last</th><th>Status</th></tr>{trs}</table>" if rows
                 else "<div class='card mut'>No letters yet. Every letter the bot writes, revises, or you edit is recorded here, "
                      "including tries you never added to the review queue.</div>")
        return "History", ("<h1>History</h1><p class='sub'>Every version of every letter: the model's drafts, its automatic revisions, "
                           "your edits, and what was sent.</p>" + table)
    vs = history.versions(conn, jid)
    if not vs:
        raise NotFound()
    parts, prev = [], None
    for v in vs:
        issues = json.loads(v["issues"] or "[]")
        flag = ("<div class='issue'>" + "<br>".join(esc(i) for i in issues) + "</div>") if issues else (
            "<div class='mut'>no fact-check issues</div>" if v["kind"] in ("draft", "revision") else "")
        shown = history.word_diff_html(prev, v["body"]) if prev is not None else esc(v["body"])
        parts.append(
            f'<div class="card"><div><b>{esc(KIND_LABEL.get(v["kind"], v["kind"]))}</b> '
            f'<span class="mut">{esc(v["at"][:16].replace("T", " "))}'
            f'{" · " + esc(v["models"]) if v["models"] else ""}</span></div>'
            f'<div class="mut">Subject: {esc(v["subject"])}</div>{flag}'
            f'<div class="letter" style="margin-top:8px">{shown}</div>'
            + ('<div class="mut" style="margin-top:6px">Changes against the version before are marked: '
               '<ins>added</ins> <del>removed</del></div>' if prev is not None else "")
            + "</div>")
        prev = v["body"]
    first = vs[0]
    head = (f"<h1>{esc(first['title'] or jid)}</h1><p class='sub'>{esc(first['company'] or '')} · "
            f"<a href='{link('/history')}'>all history</a>"
            + (f" · <a href='{link('/job', id=jid)}'>open in review</a>" if conn.execute("SELECT 1 FROM applications WHERE job_id=?", (jid,)).fetchone() else "")
            + "</p>")
    return "History", head + "".join(parts)


def page_inbox(ctx, q):
    conn = ctx["conn"]
    show_all = q.get("all") == "1"
    hide = "" if show_all else "AND m.status NOT IN ('no_action','replied')"
    rows = conn.execute(
        f"""SELECT m.id, m.status, m.category, m.subject, m.at, j.company FROM messages m
            LEFT JOIN jobs j ON j.id=m.job_id WHERE m.direction='in' {hide} ORDER BY m.id DESC""").fetchall()
    trs = "".join(
        f'<tr><td class="n">#{r["id"]}</td><td>{badge(r["status"])}</td><td class="mut">{esc(r["category"] or "-")}</td>'
        f'<td>{esc(r["company"] or "")}</td><td><a href="{link("/message", n=r["id"])}">{esc(r["subject"] or "(no subject)")}</a></td></tr>'
        for r in rows)
    table = (f"<table><tr><th></th><th>Status</th><th>Type</th><th>Company</th><th>Subject</th></tr>{trs}</table>" if rows
             else "<div class='card mut'>Nothing waiting. Use “Check replies now” on the overview.</div>")
    toggle = (f'<a href="{link("/inbox")}">hide handled</a>' if show_all else f'<a href="{link("/inbox", all=1)}">show handled too</a>')
    return "Inbox", (f"<h1>Inbox</h1><p class='sub'>Answers to your applications, matched by thread. {toggle}</p>{table}"
                     f'<form method="post" action="/replies/check" data-busy style="margin-top:18px">{hidden(ctx["tok"])}'
                     '<button>Check replies now</button> <span class="busy mut" hidden>Working…</span></form>')


def page_message(ctx, q):
    conn, tok = ctx["conn"], ctx["tok"]
    try:
        m = actions.get_incoming(conn, int(q.get("n", "")))
    except (actions.ActionError, ValueError):
        raise NotFound()
    reasons = "".join(f"<li class='issue'>{esc(r)}</li>" for r in json.loads(m["reasons"] or "[]"))
    body = (
        f"<h1>{esc(m['subject'] or '(no subject)')}</h1><p class='sub'>From {esc(m['from_addr'] or '')} · {badge(m['status'])} · "
        f"{esc(m['category'] or '-')}</p>"
        + (f"<div class='card'><b>Summary</b><br>{esc(m['summary'])}</div>" if m["summary"] else "")
        + (f"<div class='card'><b>Needs your attention</b><ul>{reasons}</ul></div>" if reasons else "")
        + f"<div class='card'><pre>{esc(m['body'] or '')}</pre></div>"
        "<h2>Your reply</h2>"
        f'<form method="post" action="/message/save">{hidden(tok, n=m["id"])}'
        f'<label>Subject</label><input type="text" name="subject" value="{esc(m["draft_subject"] or "")}">'
        f'<label>Body</label><textarea name="body" rows="14">{esc(m["draft_body"] or "")}</textarea>'
        '<div class="row"><button class="pri">Save reply</button>'
        + (f'<a class="btn" href="{link("/reply", n=m["id"])}">Review and send…</a>' if m["status"] == "draft_ready" else "")
        + "</div></form>")
    return "Message", body


def page_reply_confirm(ctx, q):
    cfg, conn, tok = ctx["cfg"], ctx["conn"], ctx["tok"]
    try:
        n = int(q.get("n", ""))
        m, to, msg = actions.prepare_reply(cfg, conn, n)
    except ValueError:
        raise NotFound()
    except actions.ActionError as e:
        return "Cannot send", f"<h1>Cannot send</h1><div class='flash err'>{esc(str(e))}</div><a class='btn' href='{link('/message', n=q.get('n'))}'>Back</a>"
    return "Send reply", (
        "<h1>Send this reply?</h1><p class='sub'>This sends a real email. It cannot be undone.</p>"
        f'<div class="card"><table><tr><th>To</th><td><b>{esc(to)}</b></td></tr><tr><th>Subject</th><td>{esc(m["draft_subject"])}</td></tr></table></div>'
        f'<div class="card"><pre>{esc(m["draft_body"])}</pre></div>'
        f'<div class="row"><form method="post" action="/reply" data-busy style="display:inline">{hidden(tok, n=n)}'
        '<button class="pri">Send now</button> <span class="busy mut" hidden>Sending…</span></form>'
        f'<a class="btn" href="{link("/message", n=n)}">Cancel</a></div>')


# --- POST handlers (each returns a redirect target, or a page) --------------------------------

def do_try(ctx, f):
    cfg = ctx["cfg"]
    text = f.get("posting", "")
    try:
        trial.require_posting_text(text)
    except SystemExit as e:
        return page_try(ctx, form=f) + (str(e),)
    title = f.get("title") or text.strip().splitlines()[0][:120]
    job = Job("manual", "x", f.get("company", ""), title, f.get("url", "").strip(), f.get("location", ""), None, text)
    job.ext_id = actions.trial_job_id(job).split(":", 1)[1]
    try:
        res = trial.analyse(make_llm(cfg), cfg, job, letter=not f.get("no_letter"))
    except Exception as e:
        return page_try(ctx, form=f) + (f"{type(e).__name__}: {e}",)
    raise Redirect(link("/try", r=stash_result(job, res, f)))   # reload-safe: a refresh does not run it again


def do_try_save(ctx, f):
    rid = f.get("r", "")
    item = get_result(rid)
    if not item:
        raise Redirect(link("/try", err="That result is no longer in memory (the server was restarted). Run it again."))
    res = item["res"]
    try:
        _, saved_id = actions.save_trial(ctx["conn"], item["job"], res["verdict"], res["raw"], res["letter"])
    except actions.ActionError as e:
        raise Redirect(link("/try", r=rid, err=str(e)))
    item["saved"] = saved_id
    raise Redirect(link("/job", id=saved_id, ok="Added to the review queue. Review the letter, edit if needed, then approve."))


def do_job_action(ctx, f):
    cfg, conn = ctx["cfg"], ctx["conn"]
    job_id, action = f.get("id", ""), f.get("action", "")
    try:
        if action == "reject":
            actions.reject(conn, job_id)
            raise Redirect(link("/queue", ok="Rejected."))
        if action == "applied":
            actions.mark_applied(conn, job_id)
            raise Redirect(link("/queue", tab="sent", ok="Marked as applied."))
        if action == "contact":
            addr = actions.set_contact_email(conn, job_id, f.get("contact_email", ""))
            raise Redirect(link("/job", id=job_id, ok=f"Applications for this job will go to {addr}." if addr else "Email cleared."))
        if action in ("save", "approve"):
            _, _, app = actions.load(conn, job_id)
            subject = f.get("subject", "").strip()
            letter = f.get("letter", "").replace("\r\n", "\n").strip()
            if app and (letter != (app["letter"] or "").strip() or subject != (app["subject"] or "").strip()):
                actions.save_letter(conn, job_id, subject, letter, cfg)
            if action == "approve":
                d = actions.approve(cfg, conn, job_id)
                raise Redirect(link("/job", id=job_id, ok=f"Approved. Folder: {d}"))
            raise Redirect(link("/job", id=job_id, ok="Saved."))
        raise actions.ActionError("unknown action")
    except actions.ActionError as e:
        raise Redirect(link("/job", id=job_id, err=str(e)))


def do_send(ctx, f):
    cfg, conn = ctx["cfg"], ctx["conn"]
    job_id = f.get("id", "")
    with SEND_LOCK:
        try:
            prep = actions.prepare_application(cfg, conn, job_id)
            reply = actions.deliver_application(cfg, conn, prep)
        except actions.ActionError as e:
            raise Redirect(link("/job", id=job_id, err=str(e)))
        except Exception as e:      # SMTP refused, network, auth: nothing was recorded as sent
            raise Redirect(link("/job", id=job_id, err=f"Not sent: {type(e).__name__}: {e}"))
    raise Redirect(link("/queue", tab="sent", ok=f"Sent to {prep.to}. Server said: {reply}"))


def do_message_save(ctx, f):
    conn = ctx["conn"]
    try:
        n = int(f.get("n", ""))
        actions.save_reply(conn, n, f.get("subject", ""), f.get("body", "").replace("\r\n", "\n"))
    except (ValueError, actions.ActionError) as e:
        raise Redirect(link("/message", n=f.get("n"), err=str(e)))
    raise Redirect(link("/message", n=n, ok="Saved."))


def do_reply_send(ctx, f):
    cfg, conn = ctx["cfg"], ctx["conn"]
    n = f.get("n", "")
    with SEND_LOCK:
        try:
            m, to, msg = actions.prepare_reply(cfg, conn, int(n))
            actions.deliver_reply(cfg, conn, m, to, msg)
        except (ValueError, actions.ActionError) as e:
            raise Redirect(link("/message", n=n, err=str(e)))
        except Exception as e:
            raise Redirect(link("/message", n=n, err=f"Not sent: {type(e).__name__}: {e}"))
    raise Redirect(link("/inbox", ok=f"Reply sent to {to}."))


def do_replies_check(ctx, f):
    cfg, conn = ctx["cfg"], ctx["conn"]
    try:
        found = replies.poll(conn, cfg)
        done = replies.process(conn, cfg, make_llm(cfg))
    except Exception as e:
        raise Redirect(link("/inbox", err=f"Could not check replies: {type(e).__name__}: {e}"))
    raise Redirect(link("/inbox", ok=f"New replies matched: {found}. Processed: {json.dumps(done)}"))


GET_ROUTES = {"/": lambda c, q: page_overview(c), "/try": page_try_view, "/queue": page_queue, "/job": page_job,
              "/send": page_send, "/facts": page_facts, "/profile": page_profile, "/sources": page_sources, "/run": page_run, "/settings": page_settings, "/history": page_history, "/postings": page_postings, "/inbox": page_inbox, "/message": page_message, "/reply": page_reply_confirm}
POST_ROUTES = {"/try/save": do_try_save, "/job/action": do_job_action, "/send": do_send, "/message/save": do_message_save,
               "/reply": do_reply_send, "/replies/check": do_replies_check, "/settings/save": do_settings_save,
               "/profile/upload": do_profile_upload, "/profile/save": do_profile_save, "/profile/discard": do_profile_discard,
               "/facts/upload": do_facts_upload, "/facts/save": do_facts_save, "/facts/discard": do_facts_discard,
               "/sources/save": do_sources_save, "/sources/check": do_sources_check, "/run/start": do_run_start,
               "/run/cancel": do_run_cancel}


# --- server -------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    home = None
    token = ""
    allowed_hosts: set = set()
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):   # keep the terminal quiet
        pass

    def _send(self, code: int, body: str, nonce: str = ""):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _guard(self) -> bool:
        if self.headers.get("Host", "") not in self.allowed_hosts:
            self._send(403, "Forbidden: this page only answers on 127.0.0.1.")
            return False
        return True

    def _render(self, title, body, path, q=None, nonce="", err_override=""):
        q = q or {}
        return layout(body, nonce, title, active_for(path, q), ok=q.get("ok", ""), err=err_override or q.get("err", ""))

    def _ctx(self):
        cfg = load_config(self.home)
        return {"cfg": cfg, "conn": db.connect(cfg.path("db")), "tok": self.token}

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        route = GET_ROUTES.get(u.path)
        nonce = secrets.token_urlsafe(12)
        if not route:
            return self._send(404, self._render("Not found", "<h1>Not found</h1>", u.path, nonce=nonce), nonce)
        ctx = self._ctx()
        try:
            title, body = route(ctx, q)
            self._send(200, self._render(title, body, u.path, q, nonce), nonce)
        except NotFound:
            self._send(404, self._render("Not found", "<h1>Not found</h1><p class='sub'>That item does not exist.</p>", u.path, nonce=nonce), nonce)
        except Exception:
            traceback.print_exc()
            self._send(500, self._render("Error", "<h1>Something went wrong</h1><pre>" + esc(traceback.format_exc()) + "</pre>", u.path, nonce=nonce), nonce)
        finally:
            ctx["conn"].close()

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        nonce = secrets.token_urlsafe(12)
        origin = self.headers.get("Origin")
        # Browsers send "Origin: null" for same-site form posts under a no-referrer policy, so
        # "null" is let through; the token below is what a foreign page cannot supply. A named
        # origin that is not this server is refused.
        if origin and origin != "null" and urlparse(origin).netloc not in self.allowed_hosts:
            return self._send(403, f"Forbidden: cross-site request (origin {esc(origin)}).")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= (MAX_UPLOAD if u.path in UPLOAD_PATHS else MAX_BODY):
            return self._send(413, "Request too large.")
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if u.path in UPLOAD_PATHS and ctype.startswith("multipart/form-data"):
            f = parse_multipart(ctype, raw)
        else:
            multi = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            f = {k: v[0] for k, v in multi.items()}
            f["_multi"] = multi   # every value of a repeated field (checkbox groups)
        if not secrets.compare_digest(f.get("_t", ""), self.token):
            return self._send(403, "Forbidden: missing or wrong token. Reload the page and try again.")
        ctx = self._ctx()
        try:
            if u.path == "/try":
                title, body, err = do_try(ctx, f)
                return self._send(200, self._render(title, body, "/try", nonce=nonce, err_override=err), nonce)
            handler = POST_ROUTES.get(u.path)
            if not handler:
                return self._send(404, self._render("Not found", "<h1>Not found</h1>", u.path, nonce=nonce), nonce)
            handler(ctx, f)
            self._redirect("/")
        except Redirect as r:
            self._redirect(r.to)
        except Exception:
            traceback.print_exc()
            self._send(500, self._render("Error", "<h1>Something went wrong</h1><pre>" + esc(traceback.format_exc()) + "</pre>", u.path, nonce=nonce), nonce)
        finally:
            ctx["conn"].close()


def make_server(home, port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"home": home, "token": secrets.token_urlsafe(24)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    p = server.server_address[1]
    handler.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    return server


def serve(home, port: int = 8765, open_browser: bool = False) -> None:
    try:
        server = make_server(home, port)
    except OSError as e:
        raise SystemExit(f"cannot listen on 127.0.0.1:{port} ({e}). Try --port with another number.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"applybot dashboard: {url}   (Ctrl-C to stop)\nOnly this computer can reach it.", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()
