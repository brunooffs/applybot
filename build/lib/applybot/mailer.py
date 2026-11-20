"""SMTP (send) and IMAP (read / Sent copy) against your own server.

The password comes only from the environment: APPLYBOT_MAIL_PASS.
Settings (host, port, security) live in config/settings.yaml under `mail`.
"""
from __future__ import annotations

import email.utils
import imaplib
import logging
import mimetypes
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

log = logging.getLogger("applybot.mail")


def _password() -> str:
    pw = os.environ.get("APPLYBOT_MAIL_PASS")
    if not pw:
        raise RuntimeError("APPLYBOT_MAIL_PASS is not set")
    return pw


def attachment_name(attachment: Path, attach_as: str | None = None) -> str:
    """The filename the recipient sees. `attach_as` (settings: mail.attach_as) replaces the
    CV file's own name, so every recruiter gets e.g. Jane-Doe-CV.pdf and never sees
    internal names like technical_lead.pdf. Path parts are stripped; the real file's
    extension is kept if attach_as has none."""
    name = Path((attach_as or "").strip()).name
    if not name:
        return attachment.name
    if not Path(name).suffix:
        name += attachment.suffix
    return name


def build_message(sender: str, to: str, subject: str, body: str, attachment: Path | None = None,
                  in_reply_to: str | None = None, references: str | None = None,
                  attach_as: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    if attachment and attachment.exists():
        shown = attachment_name(attachment, attach_as)
        ctype = mimetypes.guess_type(shown)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=shown)
    return msg


def smtp_connect(mail: dict):
    password = _password()  # fail early with a clear message, before any network call
    ctx = ssl.create_default_context()
    if mail.get("smtp_security", "ssl") == "ssl":
        s = smtplib.SMTP_SSL(mail["smtp_host"], mail["smtp_port"], timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(mail["smtp_host"], mail["smtp_port"], timeout=30)
        s.starttls(context=ctx)
    s.login(mail["username"], password)
    return s


def imap_connect(mail: dict) -> imaplib.IMAP4_SSL:
    password = _password()
    imap = imaplib.IMAP4_SSL(mail["imap_host"], mail["imap_port"], ssl_context=ssl.create_default_context())
    imap.login(mail["username"], password)
    return imap


class SMTPRefused(RuntimeError):
    pass


def send(mail: dict, msg: EmailMessage) -> str:
    """Send and return the server's final reply (on Exim: '250 OK id=<queue id>').

    Done step by step instead of send_message() so the reply to DATA, which
    carries the queue id you can look up in cPanel's Track Delivery, is not
    thrown away, and so any refusal is raised with the server's own words."""
    headers = [str(msg[h]) for h in ("To", "Cc", "Bcc") if msg[h]]  # empty values make getaddresses() return nothing
    recipients = [a for _, a in email.utils.getaddresses(headers) if a]
    if not recipients:
        raise SMTPRefused("message has no recipients")
    with smtp_connect(mail) as s:
        s.ehlo_or_helo_if_needed()
        code, resp = s.mail(email.utils.parseaddr(str(msg["From"]))[1])
        if code != 250:
            raise SMTPRefused(f"MAIL FROM refused: {code} {resp.decode(errors='replace')}")
        for r in recipients:
            code, resp = s.rcpt(r)
            if code not in (250, 251):
                raise SMTPRefused(f"recipient {r} refused: {code} {resp.decode(errors='replace')}")
        wire = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
        code, resp = s.data(wire)
        reply = f"{code} {resp.decode(errors='replace')}"
        if code != 250:
            raise SMTPRefused(f"message refused: {reply}")
        return reply


def save_to_sent(mail: dict, msg: EmailMessage) -> bool:
    """Best effort: a failure here must never undo or block a successful send."""
    try:
        imap = imap_connect(mail)
        try:
            imap.append(mail["sent_folder"], "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        finally:
            imap.logout()
        return True
    except Exception as e:
        log.warning("could not copy to Sent folder %r: %s", mail.get("sent_folder"), e)
        return False
