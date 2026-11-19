import http.client
import json
import re
import shutil
import threading
from urllib.parse import urlencode

import pytest

from applybot import actions, db, mailer, web
from applybot.config import load_config
from applybot.models import Job
from applybot.scoring import Verdict

from conftest import FakeLLM, make_raw

POSTING = "Senior backend engineer, remote in the EU. " * 8


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = load_config().root
    shutil.copytree(root / "config", home / "config")
    shutil.copytree(root / "cvs", home / "cvs")
    cfg = load_config(home)
    cfg.cv_file("senior-backend").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(web, "make_llm", lambda c: FakeLLM())
    web.RESULTS.clear()
    sent = []
    monkeypatch.setattr(mailer, "send", lambda mail, msg: sent.append(msg) or "250 OK id=abc")
    monkeypatch.setattr(mailer, "save_to_sent", lambda mail, msg: True)
    server = web.make_server(home, 0)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True).start()
    port = server.server_address[1]
    yield type("E", (), {"home": home, "cfg": cfg, "port": port, "sent": sent,
                         "token": server.RequestHandlerClass.token, "conn": lambda self: db.connect(cfg.path("db"))})()
    server.shutdown()
    server.server_close()


def req(env, method, path, form=None, host=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", env.port)
    h = {"Host": host or f"127.0.0.1:{env.port}", **(headers or {})}
    body = None
    if form is not None:
        form = {"_t": env.token, **form}
        body = urlencode(form)
        h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    text = r.read().decode()
    loc = r.getheader("Location")
    c.close()
    return r.status, text, loc


def run_try(env, form, headers=None):
    """POST to /try, then load the result page it redirects to (as a browser does)."""
    code, text, loc = req(env, "POST", "/try", form, headers=headers)
    if code == 303:
        return req(env, "GET", loc)[0], req(env, "GET", loc)[1], loc
    return code, text, loc


def seed(env, status="drafted", contact="hr@acme.test", title="Senior Backend Engineer", issues=None, cv="senior-backend"):
    conn = env.conn()
    job = Job("t", "1", "Acme", title, "https://acme.test/job", "Remote", True, "Node.js role. " * 20)
    db.upsert_job(conn, job, "scored")
    v = Verdict("Senior Backend Engineer", cv, 90.0, "auto_eligible", ["strong match, no flags"], "en", contact)
    db.save_score(conn, job.id, v, make_raw())
    db.save_application(conn, job.id, status, "Application: Backend", "Hello,\nI build backends.\nBruno Sousa",
                        {"issues": issues or []})
    conn.close()
    return job.id


def status_of(env, job_id):
    conn = env.conn()
    try:
        return conn.execute("SELECT status FROM applications WHERE job_id=?", (job_id,)).fetchone()[0]
    finally:
        conn.close()


# --- guards -----------------------------------------------------------------

def test_pages_load(env):
    for path in ("/", "/try", "/queue", "/queue?tab=approved", "/inbox"):
        code, text, _ = req(env, "GET", path)
        assert code == 200 and "applybot" in text, path


def test_wrong_host_header_is_refused(env):
    assert req(env, "GET", "/", host="evil.example")[0] == 403


def test_post_without_token_is_refused(env):
    c = http.client.HTTPConnection("127.0.0.1", env.port)
    c.request("POST", "/job/action", body="id=x&action=reject",
              headers={"Host": f"127.0.0.1:{env.port}", "Content-Type": "application/x-www-form-urlencoded"})
    assert c.getresponse().status == 403


def test_cross_site_origin_is_refused(env):
    code, _, _ = req(env, "POST", "/job/action", {"id": "x", "action": "reject"}, headers={"Origin": "https://evil.example"})
    assert code == 403


def test_browser_origin_null_is_accepted_with_token(env):
    # Real browsers send "Origin: null" on same-site form posts; the token still guards it.
    code, text, _ = run_try(env, {"posting": POSTING}, headers={"Origin": "null"})
    assert code == 200 and "Strong match" in text
    code, _, _ = run_try(env, {"posting": POSTING}, headers={"Origin": f"http://127.0.0.1:{env.port}"})
    assert code == 200


def test_posting_text_is_escaped(env):
    job_id = seed(env, title="<script>alert(1)</script>")
    code, text, _ = req(env, "GET", "/job?" + urlencode({"id": job_id}))
    assert code == 200 and "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)" in text
    assert "<script>alert(1)" not in req(env, "GET", "/queue")[1]


def test_unknown_job_is_404(env):
    assert req(env, "GET", "/job?id=nope")[0] == 404


# --- try ---------------------------------------------------------------------

def test_try_runs_and_saves_nothing(env):
    code, text, _ = run_try(env, {"posting": POSTING, "company": "Acme"})
    assert code == 200 and "Strong match" in text and "Fact-check passed" in text
    conn = env.conn()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert env.sent == []


def test_try_refuses_tiny_input(env):
    code, text, _ = req(env, "POST", "/try", {"posting": "n"})
    assert code == 200 and "not a job posting" in text


def test_try_shows_model_errors_instead_of_crashing(env, monkeypatch):
    class Boom:
        def complete_json(self, *a):
            raise RuntimeError("model is down")
    monkeypatch.setattr(web, "make_llm", lambda c: Boom())
    code, text, _ = req(env, "POST", "/try", {"posting": POSTING})
    assert code == 200 and "model is down" in text


# --- review ------------------------------------------------------------------

def test_approve_clean_draft(env):
    jid = seed(env)
    code, _, loc = req(env, "POST", "/job/action", {"id": jid, "action": "approve",
                                                  "subject": "Application: Backend", "letter": "Hello,\r\nI build backends.\r\nBruno Sousa"})
    assert code == 303 and "ok=" in loc
    assert status_of(env, jid) == "approved"
    assert (env.cfg.path("outbox")).exists()


def test_failed_factcheck_cannot_be_approved_without_edit_or_force(env):
    jid = seed(env, status="needs_edit", issues=["unsupported claim: led teams"])
    page = req(env, "GET", "/job?" + urlencode({"id": jid}))[1]
    assert "unsupported claim: led teams" in page and 'name="force"' in page
    letter = "Hello,\nI build backends.\nBruno Sousa"
    _, _, loc = req(env, "POST", "/job/action", {"id": jid, "action": "approve", "subject": "Application: Backend", "letter": letter})
    assert "err=" in loc and status_of(env, jid) == "needs_edit"
    req(env, "POST", "/job/action", {"id": jid, "action": "approve", "subject": "Application: Backend", "letter": letter, "force": "1"})
    assert status_of(env, jid) == "approved"


def test_editing_the_letter_clears_the_failure(env):
    jid = seed(env, status="needs_edit", issues=["unsupported claim: led teams"])
    req(env, "POST", "/job/action", {"id": jid, "action": "approve", "subject": "S", "letter": "A fixed letter.\r\nBruno"})
    assert status_of(env, jid) == "approved"
    conn = env.conn()
    row = conn.execute("SELECT letter, verification FROM applications WHERE job_id=?", (jid,)).fetchone()
    assert row["letter"] == "A fixed letter.\nBruno" and "edited_by_human" in row["verification"]


def test_reject(env):
    jid = seed(env)
    req(env, "POST", "/job/action", {"id": jid, "action": "reject"})
    assert status_of(env, jid) == "rejected"


# --- sending -----------------------------------------------------------------

def approve(env, jid):
    req(env, "POST", "/job/action", {"id": jid, "action": "approve", "subject": "Application: Backend",
                                     "letter": "Hello,\nI build backends.\nBruno Sousa"})


def test_send_needs_confirmation_and_sends_once(env):
    jid = seed(env)
    approve(env, jid)
    page = req(env, "GET", "/send?" + urlencode({"id": jid}))[1]
    assert "hr@acme.test" in page and "senior-backend.pdf" in page and env.sent == []   # viewing sends nothing
    code, _, loc = req(env, "POST", "/send", {"id": jid})
    assert code == 303 and "Sent+to+hr%40acme.test" in loc and len(env.sent) == 1
    assert status_of(env, jid) == "sent"
    req(env, "POST", "/send", {"id": jid})                      # a second click
    assert len(env.sent) == 1


def test_send_failure_leaves_it_approved(env, monkeypatch):
    jid = seed(env)
    approve(env, jid)
    def boom(mail, msg):
        raise mailer.SMTPRefused("535 auth failed")
    monkeypatch.setattr(mailer, "send", boom)
    _, _, loc = req(env, "POST", "/send", {"id": jid})
    assert "Not+sent" in loc and status_of(env, jid) == "approved"
    conn = env.conn()
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE direction='out'").fetchone()[0] == 0


def test_send_refused_without_cv_file(env):
    jid = seed(env)
    approve(env, jid)
    env.cfg.cv_file("senior-backend").unlink()
    _, _, loc = req(env, "POST", "/send", {"id": jid})
    assert "missing" in loc and env.sent == [] and status_of(env, jid) == "approved"


def test_send_refused_without_contact_email(env):
    jid = seed(env, contact=None)
    approve(env, jid)
    _, _, loc = req(env, "POST", "/send", {"id": jid})
    assert "no+email" in loc and env.sent == []
    req(env, "POST", "/job/action", {"id": jid, "action": "applied"})
    assert status_of(env, jid) == "sent" and env.sent == []


def test_daily_cap_is_enforced(env):
    jid = seed(env)
    approve(env, jid)
    conn = env.conn()
    for i in range(env.cfg.settings["mail"]["daily_cap"]):
        db.save_message(conn, message_id=f"<m{i}>", job_id="x", direction="out", from_addr="a", to_addr="b",
                        subject="s", body="b", status="sent")
    conn.close()
    _, _, loc = req(env, "POST", "/send", {"id": jid})
    assert "daily+cap" in loc and env.sent == [] and status_of(env, jid) == "approved"


def test_draft_that_is_not_approved_cannot_be_sent(env):
    jid = seed(env)
    _, _, loc = req(env, "POST", "/send", {"id": jid})
    assert "err=" in loc and env.sent == []


# --- replies -----------------------------------------------------------------

def seed_incoming(env, status="needs_you"):
    conn = env.conn()
    db.save_message(conn, message_id="<in1>", job_id=None, direction="in", parent_id="<out1>",
                    from_addr="Rita <rita@acme.test>", to_addr="bruno@gif.pt", subject="Quick call?",
                    body="Can we talk <b>tomorrow</b>?", status=status, category="scheduling", summary="wants a call",
                    reasons=json.dumps(["mentions tomorrow: you answer this one"]))
    n = conn.execute("SELECT id FROM messages WHERE message_id='<in1>'").fetchone()[0]
    conn.close()
    return n


def test_inbox_message_and_reply_flow(env):
    n = seed_incoming(env)
    assert "Quick call?" in req(env, "GET", "/inbox")[1]
    page = req(env, "GET", f"/message?n={n}")[1]
    assert "&lt;b&gt;tomorrow" in page and "<b>tomorrow</b>" not in page and "you answer this one" in page
    assert "Review and send" not in page                                    # nothing saved yet
    assert "err=" in req(env, "POST", "/reply", {"n": str(n)})[2] and env.sent == []
    req(env, "POST", "/message/save", {"n": str(n), "subject": "", "body": "Tomorrow works.\r\nBruno"})
    assert "rita@acme.test" in req(env, "GET", f"/reply?n={n}")[1] and env.sent == []
    code, _, loc = req(env, "POST", "/reply", {"n": str(n)})
    assert code == 303 and len(env.sent) == 1 and env.sent[0]["To"] == "rita@acme.test"
    assert env.sent[0]["Subject"].lower().startswith("re:")
    conn = env.conn()
    assert conn.execute("SELECT status FROM messages WHERE id=?", (n,)).fetchone()[0] == "replied"


def test_check_replies_reports_errors_without_crashing(env, monkeypatch):
    from applybot import replies
    monkeypatch.setattr(replies, "poll", lambda conn, cfg: (_ for _ in ()).throw(RuntimeError("APPLYBOT_MAIL_PASS is not set")))
    _, _, loc = req(env, "POST", "/replies/check", {})
    assert "Could+not+check" in loc


# --- shared actions ------------------------------------------------------------

def test_actions_refuse_unapproved_send(env):
    jid = seed(env)
    with pytest.raises(actions.ActionError):
        actions.prepare_application(env.cfg, env.conn(), jid)


# --- formatted result --------------------------------------------------------

def test_result_shows_requirement_icons_and_what_the_rules_counted(env, monkeypatch):
    reqs = [
        {"requirement": "Node.js", "status": "met", "basis": "production", "disqualifying": False},
        {"requirement": "Java 5y", "status": "unmet", "basis": "none", "disqualifying": False},
        {"requirement": "Load balancing", "status": "met", "basis": "none", "disqualifying": False},
    ]
    monkeypatch.setattr(web, "make_llm", lambda c: FakeLLM(score=make_raw(mandatory_requirements=reqs)))
    _, text, _ = run_try(env, {"posting": POSTING})
    assert 'class="ico ok"' in text and 'class="ico bad"' in text and 'class="ico warn"' in text
    assert "which the rules count as" in text           # the model said met on none -> counted unknown


def test_result_escapes_letter_and_shows_failed_factcheck(env, monkeypatch):
    letter = {"subject": "<i>S</i>", "body": "<script>alert(1)</script> I know load balancing."}
    verify = {"claims": [{"text": "load balancing", "supported": False, "evidence": "not in facts"}]}
    monkeypatch.setattr(web, "make_llm", lambda c: FakeLLM(letter=letter, verify=verify))
    _, text, _ = run_try(env, {"posting": POSTING})
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)" in text
    assert "Fact-check failed" in text and "unsupported claim: load balancing" in text
    assert 'data-copy="#letter-body"' in text


def test_score_only_skips_the_letter(env):
    _, text, _ = run_try(env, {"posting": POSTING, "no_letter": "1"})
    assert "Strong match" in text and "Letter skipped" in text and 'id="letter-body"' not in text


# --- from try into the review queue --------------------------------------------

def test_try_result_survives_navigation_and_refresh(env):
    code, text, loc = run_try(env, {"posting": POSTING, "company": "Acme"})
    assert loc.startswith("/try?r=") and "Recent results" in text
    again = req(env, "GET", loc)[1]                       # a refresh or coming back later
    assert "Strong match" in again and "Add to review queue" in again
    assert "Recent results" in req(env, "GET", "/try")[1]  # also listed on the plain page


def test_save_to_review_queue_then_approve_and_send(env):
    env.cfg.cv_file("senior-backend").write_bytes(b"%PDF fake")
    posting = POSTING + " Apply at jobs@acme.test"
    _, _, loc = run_try(env, {"posting": posting, "company": "Acme", "url": "https://acme.test/j", "title": "Backend Engineer"})
    assert "Nothing here" in req(env, "GET", "/queue")[1]     # trying a posting alone queues nothing
    rid = loc.split("r=")[1]
    code, _, jloc = req(env, "POST", "/try/save", {"r": rid})
    assert code == 303 and jloc.startswith("/job?id=manual") and "ok=" in jloc
    queue = req(env, "GET", "/queue")[1]
    assert "Backend Engineer" in queue
    job = req(env, "GET", jloc.split("&ok=")[0])[1]
    assert "https://acme.test/j" in job and "Hello" in job
    assert "in review queue" in req(env, "GET", loc)[1]    # the result page now says so
    conn = env.conn()
    app = conn.execute("SELECT status, verification FROM applications").fetchone()
    assert app["status"] == "drafted" and "from_try" in app["verification"]
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1


def test_saving_twice_does_not_duplicate(env):
    _, _, loc = run_try(env, {"posting": POSTING, "company": "Acme"})
    rid = loc.split("r=")[1]
    req(env, "POST", "/try/save", {"r": rid})
    req(env, "POST", "/try/save", {"r": rid})
    conn = env.conn()
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_failed_factcheck_is_saved_as_needs_edit(env, monkeypatch):
    verify = {"claims": [{"text": "load balancing", "supported": False, "evidence": "not in facts"}]}
    monkeypatch.setattr(web, "make_llm", lambda c: FakeLLM(verify=verify))
    _, _, loc = run_try(env, {"posting": POSTING})
    req(env, "POST", "/try/save", {"r": loc.split("r=")[1]})
    conn = env.conn()
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "needs_edit"


def test_score_only_result_cannot_be_saved(env):
    _, text, loc = run_try(env, {"posting": POSTING, "no_letter": "1"})
    assert "Add to review queue" not in text and "run it again" in text.lower()
    _, _, sloc = req(env, "POST", "/try/save", {"r": loc.split("r=")[1]})
    assert "err=" in sloc
    assert env.conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_rejected_result_is_not_queued(env, monkeypatch):
    raw = make_raw(arrangement={"remote": "yes", "portugal_eligible": "no", "employment_type": "permanent",
                                "evidence": "Senior backend engineer, remote in the EU."})
    monkeypatch.setattr(web, "make_llm", lambda c: FakeLLM(score=raw))
    _, text, loc = run_try(env, {"posting": POSTING})
    assert "Rejected postings are not queued" in text
    assert "err=" in req(env, "POST", "/try/save", {"r": loc.split("r=")[1]})[2]


def test_unknown_result_id_says_to_run_again(env):
    code, _, loc = req(env, "POST", "/try/save", {"r": "gone"})
    assert code == 303 and "no+longer+in+memory" in loc
    assert req(env, "GET", "/try?r=gone")[0] == 200


def test_saved_try_cannot_overwrite_an_approved_one(env):
    _, _, loc = run_try(env, {"posting": POSTING, "company": "Acme"})
    rid = loc.split("r=")[1]
    _, _, jloc = req(env, "POST", "/try/save", {"r": rid})
    jid = jloc.split("id=")[1].split("&")[0].replace("%3A", ":")
    req(env, "POST", "/job/action", {"id": jid, "action": "approve", "subject": "S", "letter": "Hello\nBruno"})
    web.RESULTS[rid]["saved"] = None                      # pretend we are on the result page again
    _, _, again = req(env, "POST", "/try/save", {"r": rid})
    assert "already+approved" in again


def test_models_page_lists_all_stages_and_saves(env):
    code, page, _ = req(env, "GET", "/settings")
    assert code == 200 and all(f'name="{s}"' in page for s in ("score", "draft", "revise", "verify", "reply"))
    path = env.home / "config" / "settings.yaml"
    before = path.read_text()
    code, _, loc = req(env, "POST", "/settings/save",
                       {"score": "anthropic", "draft": "ollama", "revise": "anthropic", "verify": "anthropic", "reply": "ollama"})
    assert code == 303 and "ok=" in loc
    after = path.read_text()
    import yaml
    assert yaml.safe_load(after)["llm"]["stages"] == {"score": "anthropic", "draft": "ollama", "revise": "anthropic",
                                                       "verify": "anthropic", "reply": "ollama"}
    assert "# Runtime settings." in after and "daily_cap" in after            # comments and other sections untouched
    assert (env.home / "config" / "settings.yaml.bak").read_text() == before
    import re
    assert re.search(r'name="score">.*?value="anthropic" selected', req(env, "GET", "/settings")[1])   # no restart needed


def test_models_save_rejects_unknown_provider_and_changes_nothing(env):
    path = env.home / "config" / "settings.yaml"
    before = path.read_text()
    code, _, loc = req(env, "POST", "/settings/save",
                       {"score": "gpt", "draft": "ollama", "revise": "ollama", "verify": "ollama", "reply": "ollama"})
    assert code == 303 and "err=" in loc and path.read_text() == before


def test_save_stages_inserts_block_when_missing(tmp_path):
    from applybot import actions
    from applybot.config import Config
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("llm:\n  provider: ollama\n  temperature: 0\n\nmail:\n  daily_cap: 15\n")
    cfg = Config(tmp_path, {}, {}, {}, {}, {}, [])
    actions.save_stages(cfg, {s: "ollama" for s in ("score", "draft", "revise", "verify", "reply")})
    import yaml
    d = yaml.safe_load((tmp_path / "config" / "settings.yaml").read_text())
    assert d["llm"]["stages"]["verify"] == "ollama" and d["mail"] == {"daily_cap": 15} and d["llm"]["provider"] == "ollama"


def test_history_records_try_drafts_edits_and_shows_diff(env):
    import re
    code, page, loc = run_try(env, {"posting": "Senior Backend Engineer at Acme. " + "Node.js role. " * 40, "company": "Acme"})
    assert code == 200
    conn = env.conn()
    rows = conn.execute("SELECT kind, job_id FROM letter_versions ORDER BY id").fetchall()
    assert [r["kind"] for r in rows][:1] == ["draft"]       # recorded although never added to the review queue
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    jid = rows[0]["job_id"]
    hist = req(env, "GET", "/history")[1]
    assert "Acme" in hist and "not saved" in hist
    jobpage = req(env, "GET", "/history?" + urlencode({"job": jid}))[1]
    assert "first draft" in jobpage
    # an edit after saving to the queue shows up as a new version with a visible change
    rid = re.search(r"r=([A-Za-z0-9_-]+)", loc).group(1)
    assert req(env, "POST", "/try/save", {"r": rid})[0] == 303
    req(env, "POST", "/job/action", {"id": jid, "action": "save", "subject": "S", "letter": "Totally new letter text <b>x</b>"})
    jobpage = req(env, "GET", "/history?" + urlencode({"job": jid}))[1]
    assert "edited by you" in jobpage and "<ins>" in jobpage and "<b>x</b>" not in jobpage   # escaped, not injected


def test_history_unknown_job_is_404(env):
    assert req(env, "GET", "/history?job=nope")[0] == 404


def test_history_records_what_was_sent(env):
    jid = seed(env)
    approve(env, jid)
    code, _, loc = req(env, "POST", "/send", {"id": jid})
    assert code == 303
    kinds = [r["kind"] for r in env.conn().execute("SELECT kind FROM letter_versions WHERE job_id=?", (jid,))]
    assert kinds[-1] == "sent"


def test_saving_attaches_to_existing_job_with_same_company_title_location(env):
    """An earlier paste (or a fetched posting) holds the same company+title+location under another id.
    Saving must attach the letter to it and show it in Review, not fail with 'already stored'."""
    _, _, loc = run_try(env, {"posting": POSTING, "company": "Acme", "title": "Backend Engineer"})
    rid = loc.split("r=")[1]
    item = web.get_result(rid)
    conn = env.conn()
    other = Job(source="linkedin", ext_id="123", company="Acme", title="Backend Engineer",
                location=item["job"].location, url="https://x.test", description="older text")
    assert db.upsert_job(conn, other, "scored")                # stored earlier, no application
    code, _, jloc = req(env, "POST", "/try/save", {"r": rid})
    assert code == 303 and "id=linkedin" in jloc and "ok=" in jloc
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT job_id, status FROM applications").fetchone()["job_id"] == "linkedin:123"
    assert "Backend Engineer" in req(env, "GET", "/queue")[1]


def test_saving_is_refused_if_existing_job_already_approved(env):
    _, _, loc = run_try(env, {"posting": POSTING, "company": "Acme", "title": "Backend Engineer"})
    rid = loc.split("r=")[1]
    item = web.get_result(rid)
    conn = env.conn()
    other = Job(source="linkedin", ext_id="9", company="Acme", title="Backend Engineer",
                location=item["job"].location, url="", description="x")
    db.upsert_job(conn, other, "scored")
    db.save_application(conn, other.id, "approved", "s", "b", {})
    code, _, back = req(env, "POST", "/try/save", {"r": rid})
    assert "already+approved" in back or "already%20approved" in back or "already approved" in back
