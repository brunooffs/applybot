from applybot.drafting import numeric_guard, verify_letter
from applybot.models import Job

from conftest import FakeLLM

JOB = Job(source="x", ext_id="1", company="Acme", title="Senior Backend Engineer", url="u",
          description="We need 5+ years of Node.js.")


def test_numeric_guard_allows_numbers_from_facts_and_posting():
    assert numeric_guard("20+ years and 5 years of Node.js", "experience: 20+ years", JOB.description) == []


def test_numeric_guard_flags_invented_number():
    issues = numeric_guard("I have 15 years of experience", "experience: 20+ years", "")
    assert len(issues) == 1 and "15" in issues[0]


def test_verify_flags_unsupported_claim(cfg):
    llm = FakeLLM(verify={"claims": [{"text": "AWS certified", "supported": False, "evidence": ""}]})
    issues = verify_letter(llm, {"subject": "s", "body": "I am AWS certified."}, JOB, cfg)
    assert any("AWS certified" in i for i in issues)


class RevisingLLM(FakeLLM):
    """First letter invents a number; the rewrite (which sees the problems) is clean."""

    def __init__(self):
        super().__init__()
        self.letter_calls = 0
        self.last_user = ""

    def complete_json(self, system, user):
        if "job application email" in system:
            self.letter_calls += 1
            self.last_user = user
            if self.letter_calls == 1:
                return {"subject": "s", "body": "I have 35 years of Node.js experience."}
            return {"subject": "s", "body": "I have 20+ years of experience in backend engineering."}
        return super().complete_json(system, user)


def test_failed_letter_is_revised_with_the_flagged_claims(cfg):
    from applybot.drafting import draft_checked
    llm = RevisingLLM()
    letter, issues, used = draft_checked(llm, JOB, "en", cfg)
    assert used == 1 and issues == []
    assert "20+ years" in letter["body"]
    assert "PROBLEMS FOUND" in llm.last_user and "35" in llm.last_user   # the feedback reached the model


def test_no_revision_when_allowed_zero(cfg):
    from applybot.drafting import draft_checked
    letter, issues, used = draft_checked(RevisingLLM(), JOB, "en", cfg, max_revisions=0)
    assert used == 0 and issues and "35" in letter["body"]


def test_clean_letter_is_not_revised(cfg):
    from applybot.drafting import draft_checked
    llm = FakeLLM()
    _, issues, used = draft_checked(llm, JOB, "en", cfg)
    assert used == 0 and issues == []


def test_still_failing_after_revision_stays_flagged(cfg):
    from applybot.drafting import draft_checked
    llm = FakeLLM(letter={"subject": "s", "body": "I have 35 years of experience."})
    _, issues, used = draft_checked(llm, JOB, "en", cfg)
    assert used == 1 and issues


class BrokenChecker(FakeLLM):
    """The fact-check model returns unreadable JSON."""

    def complete_json(self, system, user):
        if "fact-checker" in system:
            from applybot.llm import LLMError
            raise LLMError("invalid JSON from model: Expecting ',' delimiter")
        return super().complete_json(system, user)


def test_unreadable_checker_fails_closed_instead_of_crashing(cfg):
    from applybot.drafting import UNVERIFIED
    issues = verify_letter(BrokenChecker(), {"subject": "s", "body": "20+ years of Node.js."}, JOB, cfg)
    assert issues and issues[0].startswith(UNVERIFIED)


def test_no_revision_is_attempted_when_the_checker_itself_failed(cfg):
    from applybot.drafting import draft_checked
    llm = BrokenChecker()
    _, issues, used = draft_checked(llm, JOB, "en", cfg)
    assert used == 0 and issues


def test_verify_flags_placeholder(cfg):
    issues = verify_letter(FakeLLM(), {"subject": "s", "body": "Dear [Hiring Manager], 20+ years"}, JOB, cfg)
    assert any("placeholder" in i for i in issues)


class _Cfg:
    facts = {"not_claimed": ["C# / .NET", "Java / Spring", "Formal people management"]}


def test_not_claimed_guard_flags_listed_tech_only():
    from applybot.drafting import not_claimed_guard
    assert not_claimed_guard("I like JavaScript, Node.js and Python.", _Cfg) == []   # JavaScript is not Java
    bad = not_claimed_guard("Happy to work with Java/Spring Boot and C#.", _Cfg)
    assert any("'Java'" in b for b in bad) and any("'Spring'" in b for b in bad) and any("'C#'" in b for b in bad)


def _seed_letters(tmp_path, rows):
    import json
    import shutil
    from applybot import db
    from applybot.config import load_config
    root = load_config().root
    home = tmp_path / "home"
    shutil.copytree(root / "config", home / "config")
    shutil.copytree(root / "cvs", home / "cvs")
    cfg = load_config(home)
    conn = db.connect(cfg.path("db"))
    for i, (status, issues, edited, lang, cv, letter) in enumerate(rows):
        jid = f"t:{i}"
        conn.execute("INSERT INTO jobs (id, source, company, title, status) VALUES (?,?,?,?, 'scored')", (jid, "t", "Acme", "Eng"))
        conn.execute("INSERT INTO scores (job_id, cv_id, language) VALUES (?,?,?)", (jid, cv, lang))
        conn.execute("INSERT INTO applications (job_id, status, subject, letter, verification, updated_at) VALUES (?,?,?,?,?,?)",
                     (jid, status, "s", letter, json.dumps({"issues": issues, "edited_by_human": edited}), f"2026-11-0{i + 1}"))
    conn.commit()
    conn.close()
    return cfg


def test_examples_use_only_clean_approved_letters_in_same_language(tmp_path):
    from applybot.drafting import approved_examples
    cfg = _seed_letters(tmp_path, [
        ("sent", [], False, "en", "senior-backend", "SENT-OK"),
        ("approved", ["unsupported claim: x"], False, "en", "senior-backend", "FORCED-BAD"),
        ("approved", ["unsupported claim: x"], True, "en", "senior-backend", "EDITED-FIXED"),
        ("drafted", [], False, "en", "senior-backend", "NOT-APPROVED"),
        ("sent", [], False, "pt", "senior-backend", "WRONG-LANGUAGE"),
    ])
    got = approved_examples(cfg, JOB, "en", n=5)
    assert set(got) == {"SENT-OK", "EDITED-FIXED"}
    assert got[0] == "SENT-OK"                       # sent before merely approved


def test_examples_can_be_turned_off_and_never_break_drafting(tmp_path):
    from applybot.drafting import approved_examples, draft_letter
    cfg = _seed_letters(tmp_path, [("sent", [], False, "en", "senior-backend", "SENT-OK")])
    assert approved_examples(cfg, JOB, "en", n=0) == []
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    cfg.settings["paths"]["db"] = str(blocker / "none.db")          # parent is a file: store unreadable, no crash
    assert approved_examples(cfg, JOB, "en") == []
    seen = {}

    class Spy(FakeLLM):
        def complete_json(self, system, user):
            seen["user"] = user
            return {"subject": "s", "body": "b"}
    cfg2 = _seed_letters(tmp_path / "b", [("sent", [], False, "en", "senior-backend", "SENT-OK")])
    draft_letter(Spy(), JOB, "en", cfg2)
    assert "STYLE EXAMPLES" in seen["user"] and "SENT-OK" in seen["user"]
