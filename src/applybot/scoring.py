"""Scoring: the LLM extracts structured judgements, plain code decides.

The LLM never outputs the decision. It reports per-dimension scores, the
posting's mandatory requirements (with the basis on which Bruno meets them),
and the working arrangement. `decide()` is deterministic and unit-tested, so
a high score can never hide a disqualifying requirement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import re

import yaml

from .llm import for_stage
from .models import Job

DIMENSIONS = ["technical", "seniority", "responsibilities", "architecture_domain", "geography", "employment"]
OK_BASIS = {"production", "project", "working", "stated"}

SYSTEM = """You evaluate one job posting against a candidate's verified profile.
Output ONLY a JSON object. No prose, no markdown.

Rules:
- Judge only from the posting and the candidate data provided. Technologies not listed in the candidate facts are level "none".
- Levels: production = used in professional systems; project = independent/personal projects only; working = can work with it, limited implementation; familiarity = concepts only; none = no experience.
- A mandatory requirement phrased as professional experience or years of experience is "met" only if the candidate's basis is "production". Independent-project experience does NOT satisfy "professional experience".
- Set disqualifying=true for hard requirements: named language fluency the candidate lacks, mandatory technology with years of experience, work authorisation/residency, security clearance, formal people-management experience.
- The job title alone must not inflate scores. Score actual responsibilities.
- Principal-level roles: score on real responsibilities; organisation-wide influence and architecture governance are not established.
- portugal_eligible: "yes" if the role can be done from Portugal (remote worldwide/EU/Portugal, B2B allowed, or on-site in Portugal); "no" if it requires residency or work authorisation elsewhere; otherwise "unknown".
- Language requirements: compare with the candidate's `languages` (and `language_levels` if given). If the posting needs a fluency level the facts do not state, use status "unknown", not "met". If the facts state a level that satisfies it, use status "met" with basis "stated".
- basis "stated" is ONLY for requirements that are not skills and that the candidate data states directly: languages and language_levels, location, availability, work_authorisation, education, certifications. Never use it for a technology or years of experience.
- EVIDENCE: a rejection must be provable from the posting. For every requirement with status "unmet" and disqualifying=true, and whenever portugal_eligible is "no", copy the exact words of the posting that say so into "evidence" (verbatim, under 200 characters). If you cannot quote such words, the status is "unknown" and portugal_eligible is "unknown". Do not paraphrase and do not quote the candidate data.
- A company offering a benefit (visa assistance, relocation help) is not a requirement. Years of experience asked for are compared with the candidate's total years, not with one technology.
- family: choose exactly one name from FAMILIES, or "none" if the role is not a fit for any.
- language: ISO 639-1 code of the posting's language.
- contact_email: an application email address written in the posting, else null. Never guess one.

Schema:
{
 "family": str,
 "dimensions": {"technical":0-100,"seniority":0-100,"responsibilities":0-100,"architecture_domain":0-100,"geography":0-100,"employment":0-100},
 "mandatory_requirements": [{"requirement": str, "status": "met"|"unmet"|"unknown", "basis": "production"|"project"|"working"|"familiarity"|"none"|"stated", "disqualifying": bool, "evidence": str|null}],
 "arrangement": {"remote": "yes"|"hybrid"|"onsite"|"unknown", "portugal_eligible": "yes"|"no"|"unknown", "employment_type": "permanent"|"contract"|"b2b"|"freelance"|"unknown", "evidence": str|null},
 "language": str,
 "contact_email": str|null,
 "summary": str
}"""


@dataclass
class Verdict:
    family: str
    cv_id: str | None
    overall: float
    decision: str                      # auto_eligible | human | reject
    reasons: list[str] = field(default_factory=list)
    language: str = "en"
    contact_email: str | None = None


def build_user_prompt(job: Job, cfg) -> str:
    max_chars = cfg.settings.get("pipeline", {}).get("max_description_chars", 12000)
    facts = {k: cfg.facts.get(k) for k in ("experience", "skills", "not_claimed", "independent_projects", "leadership",
                                           "certifications", "education", "engineering_practices",
                                           "availability", "work_authorisation", "language_levels")
             if cfg.facts.get(k) is not None}
    ident = cfg.facts.get("identity") or {}   # only what matters for fit; no contact details
    facts["languages"] = ident.get("languages")
    facts["location"] = ident.get("location")
    return (
        "FAMILIES:\n" + "\n".join(f"- {n}" for n in cfg.family_names)
        + "\n\nSELECTIVE FAMILY NOTES:\n" + yaml.safe_dump(cfg.profile["families"]["selective"], allow_unicode=True)
        + "\nCANDIDATE FACTS:\n" + yaml.safe_dump(facts, allow_unicode=True, sort_keys=False)
        + "\nCANDIDATE PREFERENCES:\n" + yaml.safe_dump(cfg.profile["preferences"], allow_unicode=True)
        + f"\n\nJOB\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
          f"Remote flag: {job.remote}\nEmployment type: {job.employment_type}\n\n"
          f"{job.description[:max_chars]}"
    )


def _clamp(x) -> float:
    try:
        return max(0.0, min(100.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quote_in_posting(evidence, posting: str) -> bool:
    """True if the model's evidence really is words from the posting (ignoring case,
    punctuation and whitespace). Very short quotes do not count as proof."""
    ev = _squash(evidence if isinstance(evidence, str) else "")
    return len(ev) >= 8 and ev in _squash(posting)


def effective_status(req: dict) -> str:
    """'met' only counts if the stated basis is real. Otherwise it is unknown."""
    status = req.get("status", "unknown")
    if status not in ("met", "unmet", "unknown"):
        return "unknown"
    if status == "met" and req.get("basis", "none") not in OK_BASIS:
        return "unknown"
    return status


def weighted_overall(dims: dict, weights: dict) -> float:
    total_w = sum(weights.get(d, 0) for d in DIMENSIONS)
    if total_w <= 0:
        return 0.0
    return round(sum(_clamp(dims.get(d)) * weights.get(d, 0) for d in DIMENSIONS) / total_w, 1)


def decide(raw: dict, criteria: dict, family_to_cv: dict[str, str], valid_families: list[str],
           selective: tuple[str, ...] | list[str] = (), posting: str | None = None) -> Verdict:
    """`posting` is the text the model saw. When given, a rejection that rests on the
    model's say-so alone (no verbatim quote from the posting) is downgraded to human
    review instead of silently discarding a job. None skips that check (unit tests)."""
    th = criteria["thresholds"]
    overall = weighted_overall(raw.get("dimensions") or {}, criteria["weights"])
    family = raw.get("family") or "none"
    arr = raw.get("arrangement") or {}
    base = dict(
        overall=overall,
        language=(raw.get("language") or "en").lower()[:2],
        contact_email=raw.get("contact_email") or None,
    )

    def verdict(decision, reasons, cv=None):
        return Verdict(family=family, cv_id=cv, decision=decision, reasons=reasons, **base)

    if family not in valid_families:
        return verdict("reject", ["no matching job family"])
    unproven: list[str] = []   # rejections the model could not back with a quote
    proven = lambda ev: posting is None or quote_in_posting(ev, posting)

    if arr.get("portugal_eligible") == "no":
        if proven(arr.get("evidence")):
            return verdict("reject", ["cannot be done from Portugal"])
        unproven.append("model says it cannot be done from Portugal but quotes nothing from the posting")

    reqs = raw.get("mandatory_requirements") or []
    blockers = []
    for r in reqs:
        if r.get("disqualifying") and effective_status(r) == "unmet":
            if proven(r.get("evidence")):
                blockers.append(r.get("requirement", "?"))
            else:
                unproven.append(f"model says requirement unmet but quotes nothing from the posting: {r.get('requirement', '?')}")
    if blockers:
        return verdict("reject", [f"unmet mandatory requirement: {b}" for b in blockers])
    if overall < th["potential"]:
        return verdict("reject", [f"score {overall} below {th['potential']}"])

    cv = family_to_cv.get(family)
    review: list[str] = list(unproven)
    for r in reqs:
        st = effective_status(r)
        name = r.get("requirement", "?")
        if st == "unmet" and r.get("disqualifying"):
            continue   # already listed above as unproven
        if st == "unmet":
            review.append(f"unmet (non-blocking) requirement: {name}")
        elif st == "unknown" and r.get("disqualifying"):
            review.append(f"cannot confirm hard requirement: {name}")
    if arr.get("portugal_eligible", "unknown") == "unknown":
        review.append("unclear whether it can be done from Portugal")
    if arr.get("remote") == "onsite":
        review.append("on-site role")
    if cv is None:
        review.append(f"no CV mapped for family '{family}'")
    if family in selective:
        review.append("selective/stretch family")
    if overall < th["strong"]:
        review.append(f"score {overall} below auto threshold {th['strong']}")

    if review:
        return verdict("human", review, cv)
    return verdict("auto_eligible", ["strong match, no flags"], cv)


def score_job(llm, job: Job, cfg) -> tuple[Verdict, dict]:
    prompt = build_user_prompt(job, cfg)
    raw = for_stage(llm, "score").complete_json(SYSTEM, prompt)
    selective = list(cfg.profile["families"]["selective"].keys())
    # Evidence must come from the posting the model was shown, so check against the same text.
    seen = f"{job.title}\n{job.company}\n{job.location}\n{job.description[:cfg.settings.get('pipeline', {}).get('max_description_chars', 12000)]}"
    return decide(raw, cfg.criteria, cfg.family_to_cv, cfg.family_names, selective, posting=seen), raw
