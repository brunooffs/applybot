from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  company TEXT, title TEXT, location TEXT,
  remote INTEGER,
  url TEXT, description TEXT, employment_type TEXT, posted_at TEXT,
  fetched_at TEXT,
  dedupe_key TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'new',      -- new | filtered | scored
  filter_reason TEXT
);
CREATE TABLE IF NOT EXISTS scores (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id),
  family TEXT, cv_id TEXT, overall REAL,
  decision TEXT,                            -- auto_eligible | human | reject
  reasons TEXT,                             -- JSON list
  language TEXT, contact_email TEXT,
  raw TEXT,                                 -- raw LLM JSON, for auditing
  scored_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id),
  status TEXT NOT NULL,                     -- drafted | needs_edit | approved | rejected | sent
  subject TEXT, letter TEXT,
  verification TEXT,                        -- JSON: issues found by the checks
  packet_dir TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT UNIQUE NOT NULL,
  job_id TEXT,
  direction TEXT NOT NULL,                  -- out | in
  parent_id TEXT,                           -- Message-ID this one answers
  from_addr TEXT, to_addr TEXT, subject TEXT, body TEXT,
  at TEXT,
  category TEXT, summary TEXT,
  status TEXT,                              -- sent | new | draft_ready | needs_edit | needs_you | no_action | replied
  reasons TEXT,                             -- JSON list
  draft_subject TEXT, draft_body TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_job(conn: sqlite3.Connection, job: Job, status: str = "new", reason: str = "") -> bool:
    """Insert if unseen (by id or by company+title+location). True if inserted."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs
           (id, source, company, title, location, remote, url, description,
            employment_type, posted_at, fetched_at, dedupe_key, status, filter_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.id, job.source, job.company, job.title, job.location,
            None if job.remote is None else int(job.remote),
            job.url, job.description, job.employment_type, job.posted_at,
            now(), job.dedupe_key, status, reason,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def save_score(conn, job_id: str, v, raw: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO scores
           (job_id, family, cv_id, overall, decision, reasons, language,
            contact_email, raw, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (job_id, v.family, v.cv_id, v.overall, v.decision, json.dumps(v.reasons),
         v.language, v.contact_email, json.dumps(raw), now()),
    )
    conn.execute("UPDATE jobs SET status='scored' WHERE id=?", (job_id,))
    conn.commit()


def save_application(conn, job_id: str, status: str, subject: str, letter: str,
                     verification: dict) -> None:
    ts = now()
    conn.execute(
        """INSERT INTO applications (job_id, status, subject, letter, verification, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, subject=excluded.subject,
             letter=excluded.letter, verification=excluded.verification, updated_at=excluded.updated_at""",
        (job_id, status, subject, letter, json.dumps(verification), ts, ts),
    )
    conn.commit()


def set_application_status(conn, job_id: str, status: str, packet_dir: str | None = None) -> None:
    conn.execute(
        "UPDATE applications SET status=?, packet_dir=COALESCE(?, packet_dir), updated_at=? WHERE job_id=?",
        (status, packet_dir, now(), job_id),
    )
    conn.commit()


def resolve_job_id(conn, prefix: str) -> str:
    rows = conn.execute("SELECT id FROM jobs WHERE id LIKE ?", (prefix + "%",)).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"'{prefix}' matches {len(rows)} jobs; be more specific")
    return rows[0]["id"]


def save_message(conn, **f) -> int:
    f.setdefault("at", now())
    cols = ", ".join(f)
    cur = conn.execute(f"INSERT OR IGNORE INTO messages ({cols}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
    conn.commit()
    return cur.lastrowid if cur.rowcount else 0


def message_exists(conn, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM messages WHERE message_id=?", (message_id,)).fetchone() is not None


def find_outbound(conn, message_ids: list[str]):
    if not message_ids:
        return None
    q = ",".join("?" * len(message_ids))
    return conn.execute(
        f"SELECT * FROM messages WHERE direction='out' AND message_id IN ({q})", message_ids
    ).fetchone()


def count_sent_today(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction='out' AND substr(at,1,10)=date('now')"
    ).fetchone()[0]
