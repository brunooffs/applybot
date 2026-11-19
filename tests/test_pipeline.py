import json

from applybot import db, pipeline
from applybot.models import Job
from applybot.prefilter import prefilter
from conftest import FakeLLM, make_raw


def jobs():
    return [
        Job("t", "1", "Acme", "Senior Backend Engineer", "u1", "Remote", True, "Node.js role"),
        Job("t", "2", "Acme", "Junior Backend Engineer", "u2", "Remote", True, "x"),
        Job("t", "3", "Acme", "Pastry Chef", "u3", "Lisbon", False, "x"),
        Job("other", "9", "Acme", "Senior Backend Engineer", "u9", "Remote", True, "dup via other source"),
    ]


def test_prefilter(cfg):
    ok = [prefilter(j, cfg.criteria)[0] for j in jobs()]
    assert ok == [True, False, False, True]


def test_end_to_end_with_fake_llm(cfg):
    conn = db.connect(":memory:")
    s = pipeline.ingest(conn, cfg, jobs())
    assert s == {"seen": 4, "new": 1, "filtered": 2, "duplicate": 1}

    s = pipeline.score_pending(conn, cfg, FakeLLM())
    assert s["scored"] == 1 and s["auto_eligible"] == 1

    s = pipeline.draft_pending(conn, cfg, FakeLLM())
    assert s == {"drafted": 1, "needs_edit": 0, "errors": 0}

    # idempotent: nothing left to do
    assert pipeline.score_pending(conn, cfg, FakeLLM())["scored"] == 0
    assert pipeline.draft_pending(conn, cfg, FakeLLM())["drafted"] == 0


def test_rejected_jobs_get_no_draft(cfg):
    conn = db.connect(":memory:")
    pipeline.ingest(conn, cfg, jobs()[:1])
    bad = make_raw(mandatory_requirements=[
        {"requirement": "Java/Spring 5y", "status": "unmet", "basis": "none", "disqualifying": True,
         "evidence": "Node.js role"}])   # quote must appear in the posting
    pipeline.score_pending(conn, cfg, FakeLLM(score=bad))
    assert pipeline.draft_pending(conn, cfg, FakeLLM())["drafted"] == 0
    assert conn.execute("SELECT decision FROM scores").fetchone()["decision"] == "reject"


def test_invented_number_lands_in_needs_edit(cfg):
    conn = db.connect(":memory:")
    pipeline.ingest(conn, cfg, jobs()[:1])
    pipeline.score_pending(conn, cfg, FakeLLM())
    liar = {"subject": "s", "body": "I have 35 years of Node.js experience."}
    s = pipeline.draft_pending(conn, cfg, FakeLLM(letter=liar))
    assert s["needs_edit"] == 1
    row = conn.execute("SELECT status, verification FROM applications").fetchone()
    assert row["status"] == "needs_edit" and "35" in json.loads(row["verification"])["issues"][0]
