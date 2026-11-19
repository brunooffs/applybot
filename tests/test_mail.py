from email.message import EmailMessage

import pytest

from applybot import db, mailer, replies
from conftest import FakeLLM

MAIL = {"username": "alex@example.com", "smtp_host": "mail.example.com", "smtp_port": 465, "smtp_security": "ssl",
        "imap_host": "mail.example.com", "imap_port": 993, "sent_folder": "INBOX.Sent"}


class Rec:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what happened."""
    calls = []

    def __init__(self, name):
        self.name = name

    def __call__(self, host, port, timeout=None, context=None):
        Rec.calls.append((self.name, host, port))
        return self

    def starttls(self, context=None):
        Rec.calls.append(("starttls",))

    def login(self, user, pw):
        Rec.calls.append(("login", user))


def test_port_465_uses_implicit_ssl(monkeypatch):
    Rec.calls = []
    monkeypatch.setenv("APPLYBOT_MAIL_PASS", "x")
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", Rec("ssl"))
    mailer.smtp_connect(MAIL)
    assert Rec.calls == [("ssl", "mail.example.com", 465), ("login", "alex@example.com")]


def test_starttls_mode(monkeypatch):
    Rec.calls = []
    monkeypatch.setenv("APPLYBOT_MAIL_PASS", "x")
    monkeypatch.setattr(mailer.smtplib, "SMTP", Rec("plain"))
    mailer.smtp_connect({**MAIL, "smtp_security": "starttls", "smtp_port": 587})
    assert [c[0] for c in Rec.calls] == ["plain", "starttls", "login"]


def test_missing_password_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("APPLYBOT_MAIL_PASS", raising=False)
    with pytest.raises(RuntimeError, match="APPLYBOT_MAIL_PASS"):
        mailer.smtp_connect(MAIL)


def test_message_has_domain_message_id_and_threading():
    m = mailer.build_message("alex@example.com", "r@acme.com", "Re: Hi", "body", in_reply_to="<a@x>", references="<z@x> <a@x>")
    assert m["Message-ID"].endswith("@example.com>")
    assert m["In-Reply-To"] == "<a@x>" and m["References"] == "<z@x> <a@x>"


class FakeSMTP:
    def __init__(self, rcpt_code=250, data_code=250):
        self.rcpt_code, self.data_code, self.sent = rcpt_code, data_code, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, addr):
        return 250, b"OK"

    def rcpt(self, addr):
        return self.rcpt_code, b"rcpt reply"

    def data(self, wire):
        self.sent = wire
        return self.data_code, b"OK id=1vABC-0001-XY"


def test_send_returns_server_reply_with_queue_id(monkeypatch):
    fake = FakeSMTP()
    monkeypatch.setattr(mailer, "smtp_connect", lambda m: fake)
    msg = mailer.build_message("alex@example.com", "a@b.com", "s", "hi")
    assert mailer.send(MAIL, msg) == "250 OK id=1vABC-0001-XY"
    assert b"\r\n" in fake.sent and b"Subject: s" in fake.sent


def test_refused_recipient_raises_with_server_words(monkeypatch):
    monkeypatch.setattr(mailer, "smtp_connect", lambda m: FakeSMTP(rcpt_code=550))
    msg = mailer.build_message("alex@example.com", "a@b.com", "s", "hi")
    with pytest.raises(mailer.SMTPRefused, match="550"):
        mailer.send(MAIL, msg)


def _setup_sent(conn):
    conn.execute("INSERT INTO jobs (id, source, company, title, status) VALUES ('t:1','t','Acme','Senior Backend Engineer','scored')")
    conn.execute("INSERT INTO applications (job_id, status, subject, letter) VALUES ('t:1','sent','App','12+ years of Node.js')")
    db.save_message(conn, message_id="<out1@example.com>", job_id="t:1", direction="out", status="sent")


def _raw_reply(body, in_reply_to="<out1@example.com>", mid="<in1@acme.com>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "Rita <rita@acme.com>", "alex@example.com", "Re: App", mid
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    return m.as_bytes()


def test_only_replies_to_our_mail_are_stored():
    conn = db.connect(":memory:")
    _setup_sent(conn)
    assert replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("hello"))) == 1
    assert replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("hello"))) == 0            # duplicate
    unrelated = _raw_reply("newsletter", in_reply_to=None, mid="<other@x>")
    assert replies.store_if_reply(conn, replies.parse_incoming(unrelated)) == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE direction='in'").fetchone()[0] == 1


def test_safe_reply_gets_a_draft(cfg):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("Which stack do you use day to day?")))
    assert replies.process(conn, cfg, FakeLLM())["draft_ready"] == 1
    row = conn.execute("SELECT status, draft_body FROM messages WHERE direction='in'").fetchone()
    assert row["status"] == "draft_ready" and row["draft_body"]


@pytest.mark.parametrize("text", [
    "What are your salary expectations?",
    "What is your notice period?",
    "Do you need a visa or work permit?",
    "What is your availability next week?",
])
def test_money_dates_and_legal_are_escalated_never_drafted(cfg, text):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    replies.store_if_reply(conn, replies.parse_incoming(_raw_reply(text)))
    assert replies.process(conn, cfg, FakeLLM())["needs_you"] == 1
    assert conn.execute("SELECT draft_body FROM messages WHERE direction='in'").fetchone()["draft_body"] == ""


def test_rejection_needs_no_action(cfg):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("We went with another candidate.")))
    llm = FakeLLM(classify={"category": "rejection", "summary": "no", "needs_candidate_decision": False})
    assert replies.process(conn, cfg, llm)["no_action"] == 1


def test_model_declining_to_draft_escalates(cfg):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("Can we do a quick chat?")))
    assert replies.process(conn, cfg, FakeLLM(reply={"subject": "", "body": ""}))["needs_you"] == 1


def test_invented_number_in_reply_is_blocked(cfg):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    replies.store_if_reply(conn, replies.parse_incoming(_raw_reply("Tell me about your background.")))
    llm = FakeLLM(reply={"subject": "", "body": "I have 35 years of experience.\nAlex Example"})
    assert replies.process(conn, cfg, llm)["needs_edit"] == 1


def test_bounce_is_recognised_without_the_llm_and_marks_application(cfg):
    conn = db.connect(":memory:")
    _setup_sent(conn)
    bounce = EmailMessage()
    bounce["From"], bounce["To"] = "Mail Delivery System <MAILER-DAEMON@example.com>", "alex@example.com"
    bounce["Subject"], bounce["Message-ID"] = "Mail delivery failed: returning message to sender", "<b1@example.com>"
    bounce["In-Reply-To"] = "<out1@example.com>"
    bounce.set_content("550-5.1.1 The email account that you tried to reach does not exist.")
    assert replies.store_if_reply(conn, replies.parse_incoming(bounce.as_bytes())) == 1

    class Boom:  # any LLM call here would be a bug
        def complete_json(self, *a):
            raise AssertionError("LLM must not be called for a bounce")

    assert replies.process(conn, cfg, Boom())["bounce"] == 1
    row = conn.execute("SELECT status, category, summary FROM messages WHERE direction='in'").fetchone()
    assert row["status"] == "bounce" and "5.1.1" in row["summary"]
    assert conn.execute("SELECT status FROM applications").fetchone()["status"] == "bounced"


def test_daily_count():
    conn = db.connect(":memory:")
    assert db.count_sent_today(conn) == 0
    db.save_message(conn, message_id="<a@x>", direction="out", status="sent")
    assert db.count_sent_today(conn) == 1


def test_attach_as_hides_internal_cv_name(tmp_path):
    cv = tmp_path / "technical_lead.pdf"
    cv.write_bytes(b"%PDF-1.4 x")
    msg = mailer.build_message("alex@example.com", "a@b.com", "s", "hi", cv, attach_as="Alex-Example-CV.pdf")
    assert [p.get_filename() for p in msg.iter_attachments()] == ["Alex-Example-CV.pdf"]
    assert next(msg.iter_attachments()).get_content_type() == "application/pdf"


def test_attach_as_empty_keeps_file_name_and_sanitises(tmp_path):
    cv = tmp_path / "senior-backend.pdf"
    cv.write_bytes(b"%PDF-1.4 x")
    plain = mailer.build_message("alex@example.com", "a@b.com", "s", "hi", cv)
    assert next(plain.iter_attachments()).get_filename() == "senior-backend.pdf"
    assert mailer.attachment_name(cv, "") == "senior-backend.pdf"
    assert mailer.attachment_name(cv, "../../etc/Jane CV") == "Jane CV.pdf"
