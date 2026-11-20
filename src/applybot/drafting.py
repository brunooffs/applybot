"""Cover letter generation, grounded in facts.yaml and double-checked.

Independent guards before a letter reaches the review queue:
  1. numeric_guard: every number in the letter must already appear in
     facts.yaml or the posting (catches an invented "15 years").
  2. not_claimed_guard / placeholders: plain-code checks.
  3. parroting_guard: a run of words copied from the posting that facts.yaml
     does not contain is a requirement echoed back as a claim.
  4. LLM fact-check: every claim about the candidate must be supported by
     facts.yaml.
A letter that fails either guard is stored as `needs_edit`, never `drafted`.
"""
from __future__ import annotations

import json
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
- Anything under facts.independent_projects is independent work, never employment. Say so if you mention it.
- Do not claim any skill whose level in FACTS is "none" or that is under not_claimed. Skills at level "project" must be described as project experience, not professional.
- Do not claim to meet a posting requirement the facts do not support. Prefer to stay silent on it.
- Never name any technology listed under not_claimed, not even as something to learn or "work with". If the posting wants it, say nothing about it.
- Never write that the candidate is "excited to contribute to" or "can work with" a field or technology unless FACTS back it. Describe only what the candidate has actually done.
- Do not attach a tool to an employer unless FACTS tie them together (a tool listed under skills is not automatically used at every employer).
- Do not copy phrases from the posting. Describe what the candidate did in your own words, using FACTS wording; the posting only tells you which of the candidate's real experiences to mention.
- Mention at most 2-3 genuinely relevant points that match the posting. 150-220 words. First person, plain, no clichés, no placeholders in brackets.
- Do not mention the CV, a portfolio or GitHub at all (the CV is attached automatically); never write that they are "available on request".
- If STYLE EXAMPLES are given, they are earlier letters the candidate approved. Match their length, tone and structure. Never copy facts, employers, numbers or claims from them: facts come only from FACTS, and only what fits this posting.
- Write in the language given by LANGUAGE.
- Sign off with the candidate's name."""

VERIFY_SYSTEM = """You are a strict fact-checker. List every factual claim the LETTER makes about the candidate
(employers, roles, technologies, years, projects, certifications, achievements) and check each against FACTS.
Output ONLY JSON: {"claims":[{"text": str, "supported": bool, "evidence": str}]}
Do NOT list statements that only say the letter is an application or contact (for example that the candidate is applying, writing, thanking or looking forward to a conversation); they are not facts about the candidate. Do still check any statement of skill, experience, ability or what the candidate can contribute to. "Excited or interested to contribute to X" counts as a claim of fit for X: it is supported only if FACTS show professional-level experience in X, so a project-level skill does NOT support it.
A claim is supported only if FACTS states it directly. A "project"-level skill presented as professional experience is NOT supported.
Keep every text and evidence value under 12 words, and never use double quotation marks inside them (it breaks the JSON)."""

UNVERIFIED = "fact-check could not be completed"

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _facts_for_prompt(cfg) -> str:
    return yaml.safe_dump(cfg.facts, allow_unicode=True, sort_keys=False)


def not_claimed_guard(letter: str, cfg) -> list[str]:
    """Deterministic: a technology the candidate marked as not claimed must not appear in a letter at all.
    Entries like 'Java / Spring' are split on '/', so each name is checked on its own."""
    found = []
    for entry in cfg.facts.get("not_claimed") or []:
        for name in str(entry).split("/"):
            name = name.strip()
            if len(name) >= 2 and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", letter, re.I):
                found.append(f"letter mentions '{name}', which is listed as not claimed")
    return found


PARROT_WORDS = 5   # this many consecutive words copied from the posting count as echoing it


def _words(s: str) -> list[str]:
    return re.sub(r"[^\w+#.]+", " ", (s or "").lower().replace("’", "'")).replace(".", " ").split()


def parroting_guard(letter: str, posting: str, facts_text: str, *ignore: str) -> list[str]:
    """Deterministic: flag runs of PARROT_WORDS+ consecutive words that the letter shares with the posting
    but that facts.yaml does not contain. Small models echo requirements ("strong experience designing
    scalable distributed systems") as if they were the candidate's own claims. `ignore` texts (job title,
    company) are allowed, since naming the role is normal."""
    lw = _words(letter)
    n = PARROT_WORDS
    if len(lw) < n:
        return []
    post = " " + " ".join(_words(posting)) + " "
    facts = " " + " ".join(_words(facts_text)) + " "
    skip = " " + " ".join(" ".join(_words(t)) for t in ignore) + " "
    hits = [i for i in range(len(lw) - n + 1)
            if (g := " " + " ".join(lw[i:i + n]) + " ") in post and g not in facts and g not in skip]
    out, start, prev = [], None, None
    for i in hits + [None]:
        if start is None:
            start = prev = i
            continue
        if i is not None and i <= prev + 1:
            prev = i
            continue
        out.append(" ".join(lw[start:prev + n]))
        start = prev = i
    return [f"letter copies the posting's wording, which facts do not contain: '{p}'" for p in out if p]


def deterministic_issues(letter: str, job_title: str, job_company: str, job_description: str, cfg) -> list[str]:
    """Every plain-code check. Also re-run when a human saves an edited letter, so an edit can
    remove a problem but never hide one."""
    facts = _facts_for_prompt(cfg)
    issues = numeric_guard(letter, facts, job_description, job_title)
    issues += not_claimed_guard(letter, cfg)
    issues += parroting_guard(letter, job_description, facts, job_title, job_company)
    if "TODO" in letter or "[" in letter:
        issues.append("letter contains a placeholder")
    return issues


def numeric_guard(letter: str, *allowed_sources: str) -> list[str]:
    allowed = set()
    for s in allowed_sources:
        allowed.update(_NUM.findall(s or ""))
    return [f"number '{n}' in letter is not in facts or posting" for n in _NUM.findall(letter) if n not in allowed]


def approved_examples(cfg, job: Job, language: str, n: int | None = None) -> list[str]:
    """Letters the candidate already approved or sent, as style examples for the drafter.

    Only letters that passed the fact-check with no remaining issues are used (a letter forced
    through a failed check is never shown as a model), in the same language, never the job being
    drafted. Same CV family first, then sent before merely approved, newest first. The model is
    not trained on them; they travel inside the prompt for this one request."""
    if n is None:
        n = int(cfg.settings.get("pipeline", {}).get("example_letters", 2))
    if n <= 0:
        return []
    from . import db
    try:
        conn = db.connect(cfg.path("db"))
        try:
            rows = conn.execute(
                """SELECT a.letter, a.verification, a.status, s.cv_id, a.updated_at FROM applications a
                   JOIN scores s ON s.job_id = a.job_id
                   WHERE a.status IN ('approved','sent') AND a.job_id != ? AND s.language = ?
                     AND a.letter IS NOT NULL AND a.letter != ''""", (job.id, language)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []   # examples are an extra; their absence must never stop a draft
    want_cv = None
    try:
        conn = db.connect(cfg.path("db"))
        try:
            r = conn.execute("SELECT cv_id FROM scores WHERE job_id=?", (job.id,)).fetchone()
            want_cv = r["cv_id"] if r else None
        finally:
            conn.close()
    except Exception:
        pass
    good = []
    for r in rows:
        try:
            v = json.loads(r["verification"] or "{}")
        except ValueError:
            continue
        if v.get("issues") and not v.get("edited_by_human"):
            continue   # an unedited letter that failed the check is never a model
        good.append(r)
    good.sort(key=lambda r: (r["cv_id"] == want_cv, r["status"] == "sent", r["updated_at"] or ""), reverse=True)
    return [r["letter"].strip() for r in good[:n]]


def draft_letter(llm, job: Job, language: str, cfg) -> dict:
    user = (
        f"LANGUAGE: {language}\n\nFACTS:\n{_facts_for_prompt(cfg)}\n"
        f"\nPOSTING\nTitle: {job.title}\nCompany: {job.company}\n\n{job.description[:6000]}"
    )
    examples = approved_examples(cfg, job, language)
    if examples:
        user += "\n\nSTYLE EXAMPLES (approved earlier; match tone and structure only):\n" + "\n\n---\n\n".join(examples)
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


def draft_checked(llm, job: Job, language: str, cfg, max_revisions: int | None = None, record=None):
    """Draft, fact-check, and if it fails feed the flagged claims back for a rewrite.
    Returns (letter, remaining_issues, revisions_used). `record(kind, letter, issues)` is called after the
    first draft and after each revision, so the history keeps every version. A letter that still has
    issues after the allowed revisions stays flagged for a human."""
    if max_revisions is None:
        max_revisions = cfg.settings.get("pipeline", {}).get("max_revisions", 1)
    letter = draft_letter(llm, job, language, cfg)
    issues = verify_letter(llm, letter, job, cfg)
    if record:
        record("draft", letter, issues)
    used = 0
    # A rewrite cannot help when the checker itself failed, so don't spend calls on it.
    while issues and used < max_revisions and not any(i.startswith(UNVERIFIED) for i in issues):
        letter = revise_letter(llm, job, language, cfg, letter, issues)
        issues = verify_letter(llm, letter, job, cfg)
        used += 1
        if record:
            record("revision", letter, issues)
    return letter, issues, used


def verify_letter(llm, letter: dict, job: Job, cfg) -> list[str]:
    issues = deterministic_issues(letter["body"], job.title, job.company, job.description, cfg)
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
