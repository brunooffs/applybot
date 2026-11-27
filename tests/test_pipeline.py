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


def test_prefilter_drops_postings_located_outside_your_authorised_regions(cfg):
    we = {"home_country": "Portugal", "authorised_regions": ["Portugal", "EU"], "accepts_relocation": False}
    j = lambda loc, desc="Node.js role": Job("t", loc, "Acme", "Senior Backend Engineer", "u", loc, True, desc)   # noqa: E731
    ok = lambda loc, desc="Node.js role", w=we: prefilter(j(loc, desc), cfg.criteria, w)[0]   # noqa: E731
    assert not ok("Remote US") and not ok("New York, United States") and not ok("Remote - Canada")
    reason = prefilter(j("Remote US"), cfg.criteria, we)[1]
    assert "outside your authorised regions" in reason
    assert ok("Remote - Europe") and ok("Lisbon, Portugal") and ok("Remote EMEA") and ok("Worldwide")
    assert ok("Remote US; Remote Spain")                    # one reachable place keeps it in play
    assert ok("Remote") and ok("")                          # nothing recognisable: the scorer decides
    assert ok("Remote US", "We hire contractors via an employer of record")   # open to contractors: never dropped here
    assert ok("Remote US", w=None)                          # no eligibility configured: location is not checked
    assert ok("Remote US", w={**we, "accepts_relocation": True})
    off = {**cfg.criteria, "prefilter": {**cfg.criteria["prefilter"], "skip_unreachable_locations": False}}
    assert prefilter(j("Remote US"), off, we)[0]
