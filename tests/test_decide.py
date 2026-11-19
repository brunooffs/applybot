from applybot.scoring import DIMENSIONS, decide, effective_status, weighted_overall
from conftest import make_raw


def run(cfg, raw):
    sel = list(cfg.profile["families"]["selective"])
    return decide(raw, cfg.criteria, cfg.family_to_cv, cfg.family_names, sel)


def test_weighted_overall_uniform(cfg):
    assert weighted_overall(make_raw()["dimensions"], cfg.criteria["weights"]) == 90.0


def test_strong_clean_match_is_auto_eligible(cfg):
    v = run(cfg, make_raw())
    assert v.decision == "auto_eligible"
    assert v.cv_id == "senior-backend"


def test_high_score_cannot_hide_disqualifying_requirement(cfg):
    raw = make_raw(mandatory_requirements=[
        {"requirement": "5 years professional C#/.NET", "status": "unmet", "basis": "none", "disqualifying": True}])
    v = run(cfg, raw)
    assert v.overall >= 85
    assert v.decision == "reject"
    assert "C#" in v.reasons[0]


def test_project_experience_does_not_satisfy_met_claim_without_basis(cfg):
    # model says "met" but basis is only familiarity -> treated as unknown -> review
    raw = make_raw(mandatory_requirements=[
        {"requirement": "Kafka", "status": "met", "basis": "familiarity", "disqualifying": True}])
    assert effective_status(raw["mandatory_requirements"][0]) == "unknown"
    assert run(cfg, raw).decision == "human"


def test_stated_basis_counts_for_language_and_location_requirements(cfg):
    # "Advanced English": the facts state C2. That is not a skill basis, but it is a real one.
    req = {"requirement": "Advanced English", "status": "met", "basis": "stated", "disqualifying": True}
    assert effective_status(req) == "met"
    v = run(cfg, make_raw(mandatory_requirements=[req]))
    assert v.decision == "auto_eligible"


def test_met_with_basis_none_is_still_unknown(cfg):
    req = {"requirement": "Advanced English", "status": "met", "basis": "none", "disqualifying": True}
    assert effective_status(req) == "unknown"
    assert run(cfg, make_raw(mandatory_requirements=[req])).decision == "human"


def test_prompt_tells_the_model_when_to_use_stated(cfg):
    from applybot.scoring import SYSTEM
    assert '"stated"' in SYSTEM and "language_levels" in SYSTEM


def test_not_eligible_from_portugal_rejects(cfg):
    raw = make_raw(arrangement={"remote": "yes", "portugal_eligible": "no", "employment_type": "permanent"})
    assert run(cfg, raw).decision == "reject"


def test_unknown_eligibility_goes_to_human(cfg):
    raw = make_raw(arrangement={"remote": "yes", "portugal_eligible": "unknown", "employment_type": "permanent"})
    assert run(cfg, raw).decision == "human"


def test_onsite_goes_to_human(cfg):
    raw = make_raw(arrangement={"remote": "onsite", "portugal_eligible": "yes", "employment_type": "permanent"})
    assert run(cfg, raw).decision == "human"


def test_low_score_rejects(cfg):
    raw = make_raw(dimensions={d: 30 for d in make_raw()["dimensions"]})
    assert run(cfg, raw).decision == "reject"


def test_mid_score_goes_to_human(cfg):
    raw = make_raw(dimensions={d: 70 for d in make_raw()["dimensions"]})
    assert run(cfg, raw).decision == "human"


def test_unmapped_family_is_never_auto(cfg):
    import copy
    cfg2 = copy.copy(cfg)
    cfg2.cvs = [c for c in cfg.cvs if "Technical Lead" not in c.get("families", [])]
    v = run(cfg2, make_raw(family="Technical Lead"))
    assert v.cv_id is None


def test_selective_family_never_auto(cfg):
    v = run(cfg, make_raw(family="Project Manager"))
    assert v.decision == "human"


def test_unknown_family_rejects(cfg):
    assert run(cfg, make_raw(family="Sales Manager")).decision == "reject"
    assert run(cfg, make_raw(family="none")).decision == "reject"


# --- evidence-backed rejection --------------------------------------------

POSTING = "We need 5 years of Kotlin in production. Candidates must hold a US work permit."


def run_p(cfg, raw, posting=POSTING):
    sel = list(cfg.profile["families"]["selective"])
    return decide(raw, cfg.criteria, cfg.family_to_cv, cfg.family_names, sel, posting=posting)


def test_rejection_with_real_quote_stands(cfg):
    req = {"requirement": "US work permit", "status": "unmet", "basis": "none", "disqualifying": True,
           "evidence": "must hold a US work permit"}
    assert run_p(cfg, make_raw(mandatory_requirements=[req])).decision == "reject"


def test_rejection_without_quote_goes_to_human(cfg):
    req = {"requirement": "Visa assistance", "status": "unmet", "basis": "none", "disqualifying": True}
    v = run_p(cfg, make_raw(mandatory_requirements=[req]))
    assert v.decision == "human"
    assert any("quotes nothing" in r for r in v.reasons)


def test_invented_quote_is_not_evidence(cfg):
    req = {"requirement": "US work permit", "status": "unmet", "basis": "none", "disqualifying": True,
           "evidence": "visa sponsorship is not available"}
    assert run_p(cfg, make_raw(mandatory_requirements=[req])).decision == "human"


def test_not_eligible_needs_a_quote_too(cfg):
    arr = {"remote": "yes", "portugal_eligible": "no", "employment_type": "permanent"}
    assert run_p(cfg, make_raw(arrangement=arr)).decision == "human"
    arr["evidence"] = "Candidates must hold a US work permit"
    assert run_p(cfg, make_raw(arrangement=arr)).decision == "reject"


def test_quote_match_ignores_case_and_punctuation(cfg):
    from applybot.scoring import quote_in_posting
    assert quote_in_posting("MUST hold a US work-permit.", POSTING)
    assert not quote_in_posting("US", POSTING)          # too short to prove anything
    assert not quote_in_posting(None, POSTING)


def test_low_score_still_rejects_even_with_unproven_flags(cfg):
    raw = make_raw(dimensions={d: 20 for d in DIMENSIONS})
    assert run_p(cfg, raw).decision == "reject"
