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
