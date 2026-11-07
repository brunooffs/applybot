import pytest

from applybot.config import load_config


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    c = load_config()
    # Never let a test touch the real database (letter history is written during drafting).
    c.settings["paths"]["db"] = str(tmp_path_factory.mktemp("db") / "test.sqlite")
    return c


def make_raw(**over):
    raw = {
        "family": "Senior Backend Engineer",
        "dimensions": {d: 90 for d in
                       ["technical", "seniority", "responsibilities", "architecture_domain", "geography", "employment"]},
        "mandatory_requirements": [
            {"requirement": "Node.js", "status": "met", "basis": "production", "disqualifying": False},
        ],
        "arrangement": {"remote": "yes", "portugal_eligible": "yes", "employment_type": "permanent"},
        "language": "en",
        "contact_email": None,
        "summary": "x",
    }
    raw.update(over)
    return raw


class FakeLLM:
    """Routes by system prompt so one fake serves score, letter and verify."""

    def __init__(self, score=None, letter=None, verify=None, classify=None, reply=None):
        self.classify = classify or {"category": "info_request", "summary": "asks about stack",
                                     "needs_candidate_decision": False}
        self.reply = reply or {"subject": "", "body": "Thanks for getting back to me. Happy to share more.\nBruno Sousa"}
        self.score = score or make_raw()
        self.letter = letter or {"subject": "Application: Senior Backend Engineer",
                                 "body": "Hello,\nI have 20+ years of experience building backend systems with Node.js.\nBruno Sousa"}
        self.verify = verify or {"claims": [{"text": "20+ years", "supported": True, "evidence": "facts"}]}

    def complete_json(self, system, user):
        if "fact-checker" in system:
            return self.verify
        if "triage replies" in system:
            return self.classify
        if "write a reply email" in system:
            return self.reply
        if "job application email" in system:
            return self.letter
        return self.score
