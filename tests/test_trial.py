from applybot import trial
from applybot.models import Job
from conftest import FakeLLM, make_raw

JOB = Job("manual", "try", "Acme", "Senior Backend Engineer", "", "Remote", None, "Node.js role, remote.")


def test_tiny_input_is_refused_before_any_api_call():
    import pytest
    with pytest.raises(SystemExit, match="not a job posting"):
        trial.require_posting_text("n")
    trial.require_posting_text("x" * 300)   # long enough passes


def test_scoring_prompt_includes_languages_and_location(cfg):
    from applybot.scoring import build_user_prompt
    p = build_user_prompt(JOB, cfg)
    assert "languages:" in p and "Portuguese" in p and "location:" in p
    # a posting asking for a certificate must see them, whatever facts file is in use
    import copy
    cfg2 = copy.copy(cfg)
    cfg2.facts = {**cfg.facts, "certifications": [{"name": "Cert X"}], "education": [{"institution": "School Y"}]}
    p2 = build_user_prompt(JOB, cfg2)
    assert "Cert X" in p2 and "School Y" in p2
    assert "@" not in p.split("JOB")[0].replace("CANDIDATE PREFERENCES", "")   # no email address leaks into scoring


def test_explain_shows_cv_decision_and_letter(cfg):
    out = trial.explain(FakeLLM(), cfg, JOB)
    assert "CV       senior-backend" in out
    assert "DECISION auto_eligible" in out
    assert "--- LETTER (en) ---" in out and "VERIFICATION ok" in out


def test_explain_shows_why_a_claim_does_not_count(cfg):
    raw = make_raw(mandatory_requirements=[
        {"requirement": "Kafka in production", "status": "met", "basis": "familiarity", "disqualifying": True}])
    out = trial.explain(FakeLLM(score=raw), cfg, JOB, letter=False)
    assert "Kafka in production: met on familiarity -> unknown" in out
    assert "LETTER" not in out


def test_rejected_job_gets_no_letter(cfg):
    raw = make_raw(arrangement={"remote": "yes", "eligible": "no", "employment_type": "permanent",
                                 "evidence": "Node.js role, remote."})
    out = trial.explain(FakeLLM(score=raw), cfg, JOB)
    assert "DECISION reject" in out and "skipped" in out


def test_failed_verification_is_visible(cfg):
    llm = FakeLLM(letter={"subject": "s", "body": "I have 35 years of experience."})
    out = trial.explain(llm, cfg, JOB)
    assert "FAILED" in out and "35" in out
