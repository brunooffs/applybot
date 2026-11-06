"""Cover letter generation, grounded in facts.yaml and double-checked.

Two independent guards before a letter reaches the review queue:
  1. numeric_guard: every number in the letter must already appear in
     facts.yaml or the posting (catches an invented "15 years").
  2. LLM fact-check: every claim about the candidate must be supported by
     facts.yaml.
A letter that fails either guard is stored as `needs_edit`, never `drafted`.
"""
from __future__ import annotations

import re

import yaml

from .llm import LLMError, for_stage
from .models import Job

LETTER_SYSTEM = """You write the body of a job application email for the candidate.
Output ONLY JSON: {"subject": str, "body": str}

Hard rules:
- Use ONLY information in FACTS. Never invent employers, dates, numbers, technologies, certifications or achievements.
- Years of experience may only be stated exactly as in facts.experience.total.
- Do not mention any certification (facts.certifications is the complete list).
- Waybound, AdLab and the application-modelling platform are independent projects, never employment. Say so if you mention them.
- Do not claim any skill whose level in FACTS is "none" or that is under not_claimed. Skills at level "project" must be described as project experience, not professional.
- Do not claim to meet a posting requirement the facts do not support. Prefer to stay silent on it.
- Mention at most 2-3 genuinely relevant points that match the posting. 150-220 words. First person, plain, no clichés, no placeholders in brackets.
- Write in the language given by LANGUAGE.
- Sign off with the candidate's name."""

VERIFY_SYSTEM = """You are a strict fact-checker. List every factual claim the LETTER makes about the candidate
(employers, roles, technologies, years, projects, certifications, achievements) and check each against FACTS.
Output ONLY JSON: {"claims":[{"text": str, "supported": bool, "evidence": str}]}
A claim is supported only if FACTS states it directly. A "project"-level skill presented as professional experience is NOT supported.
Keep every text and evidence value under 12 words, and never use double quotation marks inside them (it breaks the JSON)."""

UNVERIFIED = "fact-check could not be completed"

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _facts_for_prompt(cfg) -> str:
    return yaml.safe_dump(cfg.facts, allow_unicode=True, sort_keys=False)


def numeric_guard(letter: str, *allowed_sources: str) -> list[str]:
    allowed = set()
    for s in allowed_sources:
        allowed.update(_NUM.findall(s or ""))
    return [f"number '{n}' in letter is not in facts or posting" for n in _NUM.findall(letter) if n not in allowed]


def draft_letter(llm, job: Job, language: str, cfg) -> dict:
    user = (
        f"LANGUAGE: {language}\n\nFACTS:\n{_facts_for_prompt(cfg)}\n"
        f"\nPOSTING\nTitle: {job.title}\nCompany: {job.company}\n\n{job.description[:6000]}"
    )
    out = for_stage(llm, "draft").complete_json(LETTER_SYSTEM, user)
    return {"subject": str(out.get("subject", "")).strip(), "body": str(out.get("body", "")).strip()}


def revise_letter(llm, job: Job, language: str, cfg, letter: dict, issues: list[str]) -> dict:
    """Ask for a rewrite that removes exactly the claims the fact-check rejected."""
    user = (
        f"LANGUAGE: {language}\n\nFACTS:\n{_facts_for_prompt(cfg)}\n"
        f"\nPOSTING\nTitle: {job.title}\nCompany: {job.company}\n\n{job.description[:6000]}\n"
        f"\nPREVIOUS LETTER:\n{letter['body']}\n"
        "\nPROBLEMS FOUND BY THE FACT-CHECK:\n" + "\n".join(f"- {i}" for i in issues)
        + "\n\nRewrite the letter. Remove or reword every flagged claim so that only FACTS support it. "
          "Do not add new claims, numbers or experience. Keep everything that was fine."
    )
    out = for_stage(llm, "revise").complete_json(LETTER_SYSTEM, user)
    return {"subject": str(out.get("subject", "")).strip() or letter["subject"],
            "body": str(out.get("body", "")).strip()}


def draft_checked(llm, job: Job, language: str, cfg, max_revisions: int | None = None):
    """Draft, fact-check, and if it fails feed the flagged claims back for a rewrite.
    Returns (letter, remaining_issues, revisions_used). A letter that still has
    issues after the allowed revisions stays flagged for a human."""
    if max_revisions is None:
        max_revisions = cfg.settings.get("pipeline", {}).get("max_revisions", 1)
    letter = draft_letter(llm, job, language, cfg)
    issues = verify_letter(llm, letter, job, cfg)
    used = 0
    # A rewrite cannot help when the checker itself failed, so don't spend calls on it.
    while issues and used < max_revisions and not any(i.startswith(UNVERIFIED) for i in issues):
        letter = revise_letter(llm, job, language, cfg, letter, issues)
        issues = verify_letter(llm, letter, job, cfg)
        used += 1
    return letter, issues, used


def verify_letter(llm, letter: dict, job: Job, cfg) -> list[str]:
    issues = numeric_guard(letter["body"], _facts_for_prompt(cfg), job.description, job.title)
    if "TODO" in letter["body"] or "[" in letter["body"]:
        issues.append("letter contains a placeholder")
    try:
        res = for_stage(llm, "verify").complete_json(VERIFY_SYSTEM, f"FACTS:\n{_facts_for_prompt(cfg)}\n\nLETTER:\n{letter['body']}")
    except LLMError as e:
        # Fail closed: an unreadable checker must never count as "verified".
        issues.append(f"{UNVERIFIED} ({e}); letter is NOT verified")
        return issues
    for c in res.get("claims", []):
        if not c.get("supported", False):
            issues.append(f"unsupported claim: {c.get('text', '?')}")
    return issues
