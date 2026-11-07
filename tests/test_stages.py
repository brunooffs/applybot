import pytest

from applybot.drafting import draft_checked
from applybot.llm import LLM, LLMAPIError, for_stage
from applybot.models import Job


def settings(**llm):
    base = {"provider": "ollama", "ollama": {"host": "h", "model": "m"}, "anthropic": {"model": "x"}}
    base.update(llm)
    return {"llm": base}


def test_stage_uses_override_and_default():
    llm = LLM(settings(stages={"draft": "ollama", "verify": "anthropic"}))
    assert llm.stage("draft").provider == "ollama"
    assert llm.stage("verify").provider == "anthropic"
    assert llm.stage("score").provider == "ollama"          # not listed -> default
    assert llm.stage("verify") is llm.stage("verify")       # one client per provider
    assert llm.describe() == "score=ollama  draft=ollama  revise=ollama  verify=anthropic  reply=ollama"


def test_no_stages_block_means_one_provider():
    llm = LLM(settings())
    assert all(llm.stage(s) is llm for s in ("score", "draft", "revise", "verify", "reply"))


@pytest.mark.parametrize("stages", [{"verfy": "anthropic"}, {"verify": "openai"}])
def test_typos_in_routing_are_rejected(stages):
    with pytest.raises(LLMAPIError):
        LLM(settings(stages=stages))


def test_unknown_default_provider_is_rejected():
    with pytest.raises(LLMAPIError):
        LLM(settings(provider="olama"))


def test_for_stage_passes_plain_fakes_through():
    class Fake: pass
    f = Fake()
    assert for_stage(f, "verify") is f


class Rec:
    def __init__(self, name, log, reply):
        self.name, self.log, self.reply = name, log, reply

    def complete_json(self, system, user):
        self.log.append(self.name)
        return self.reply


class Router:
    def __init__(self, drafter, checker):
        self.d, self.c = drafter, checker

    def stage(self, name):
        return self.c if name == "verify" else self.d


def test_draft_goes_to_one_model_and_fact_check_to_another(cfg):
    log = []
    drafter = Rec("draft", log, {"subject": "Hi", "body": "Plain letter with no numbers."})
    checker = Rec("verify", log, {"claims": [{"text": "x", "supported": True}]})
    job = Job(source="t", ext_id="1", company="Co", title="Dev", url="", description="Build things.")
    letter, issues, used = draft_checked(Router(drafter, checker), job, "en", cfg)
    assert log == ["draft", "verify"] and issues == [] and used == 0


def test_revise_follows_draft_unless_set_separately():
    only_draft = LLM(settings(provider="anthropic", stages={"draft": "ollama"}))
    assert only_draft.stage("revise").provider == "ollama"
    own = LLM(settings(stages={"draft": "ollama", "revise": "anthropic"}))
    assert own.stage("revise").provider == "anthropic" and own.stage("draft").provider == "ollama"


def test_rewrite_goes_to_the_revise_model(cfg):
    from applybot.drafting import revise_letter
    log = []
    class R:
        def __init__(self, n): self.n = n
        def complete_json(self, s, u):
            log.append(self.n); return {"subject": "S", "body": "Fixed."}
    class Router:
        def stage(self, name): return R(name)
    job = Job(source="t", ext_id="1", company="Co", title="Dev", url="", description="x")
    revise_letter(Router(), job, "en", cfg, {"subject": "S", "body": "old"}, ["unsupported claim: x"])
    assert log == ["revise"]


def test_timeout_becomes_a_readable_error_naming_the_provider(monkeypatch):
    import httpx
    from applybot import llm as llm_mod

    def boom(*a, **k):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(llm_mod.httpx, "post", boom)
    settings = {"llm": {"provider": "ollama", "ollama": {"host": "http://x", "model": "m", "timeout_seconds": 7}, "anthropic": {"model": "c"}}}
    with pytest.raises(LLMAPIError, match=r"Ollama did not answer within 7 seconds"):
        llm_mod.LLM(settings).complete_json("s", "u")
