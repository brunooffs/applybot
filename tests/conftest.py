import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Hermetic test home: built from the committed *.example files, so the suite never
# depends on (or touches) a developer's own config, CVs or database.
_DEFAULTS = Path(__file__).resolve().parents[1] / "src" / "applybot" / "defaults"
_HOME = Path(tempfile.mkdtemp(prefix="applybot-test-home-"))
(_HOME / "config").mkdir()
(_HOME / "cvs").mkdir()
for _f in (_DEFAULTS / "config").iterdir():
    _dst = _f.name[:-len(".example")] if _f.name.endswith(".example") else _f.name
    shutil.copy(_f, _HOME / "config" / _dst)
shutil.copy(Path(__file__).parent / "fixtures" / "cvs.yaml", _HOME / "cvs" / "cvs.yaml")
os.environ["APPLYBOT_HOME"] = str(_HOME)

from applybot.config import load_config  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


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
        "arrangement": {"remote": "yes", "eligible": "yes", "employment_type": "permanent"},
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
        self.reply = reply or {"subject": "", "body": "Thanks for getting back to me. Happy to share more.\nAlex Example"}
        self.score = score or make_raw()
        self.letter = letter or {"subject": "Application: Senior Backend Engineer",
                                 "body": "Hello,\nI have 12+ years of experience building backend systems with Node.js.\nAlex Example"}
        self.verify = verify or {"claims": [{"text": "12+ years", "supported": True, "evidence": "facts"}]}

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
