"""`applybot try`: run the whole judgement on one pasted posting and explain it.

Nothing is stored and nothing is sent. Use it to see which CV would be chosen
and why, before trusting the pipeline with real boards.
"""
from __future__ import annotations

import time

from . import history
from .drafting import draft_checked
from .models import Job
from .scoring import DIMENSIONS, effective_status, score_job

LANGS = {"pt", "en", "es", "fr", "nl"}
MIN_POSTING_CHARS = 200


def require_posting_text(text: str) -> None:
    """Refuse input too short to be a job posting, before any API call is spent."""
    n = len(text.strip())
    if n < MIN_POSTING_CHARS:
        raise SystemExit(f"only {n} characters received; that is not a job posting. "
                         "Copy the full text of the posting and try again.")


def analyse(llm, cfg, job: Job, letter: bool = True) -> dict:
    """Run the whole judgement and return it as data. `explain` turns it into text
    for the terminal; the dashboard turns the same data into a page."""
    t0 = time.time()
    verdict, raw = score_job(llm, job, cfg)
    t_score = time.time() - t0
    cvf = cfg.cv_file(verdict.cv_id) if verdict.cv_id else None
    res = {
        "models": llm.describe() if hasattr(llm, "describe") else "",
        "title": job.title, "company": job.company,
        "family": verdict.family, "cv_id": verdict.cv_id,
        "cv_file": cvf.name if cvf else None, "cv_found": bool(cvf and cvf.exists()),
        "overall": verdict.overall, "decision": verdict.decision, "reasons": list(verdict.reasons),
        "thresholds": cfg.criteria.get("thresholds", {}),
        "dimensions": {d: (raw.get("dimensions") or {}).get(d) for d in DIMENSIONS},
        "requirements": [
            {"requirement": r.get("requirement", "?"), "status": r.get("status"), "basis": r.get("basis"),
             "effective": effective_status(r), "disqualifying": bool(r.get("disqualifying")),
             "evidence": str(r["evidence"])[:160] if r.get("evidence") else ""}
            for r in (raw.get("mandatory_requirements") or [])],
        "arrangement": raw.get("arrangement") or {},
        "language": verdict.language, "contact": verdict.contact_email,
        "letter_state": "off", "letter": None, "seconds": {"score": t_score},
        "verdict": verdict, "raw": raw,      # kept so a result can be saved to the review queue
    }
    if not letter:
        res["seconds"]["total"] = time.time() - t0
        return res
    if verdict.decision == "reject":
        res["letter_state"] = "skipped"
        res["seconds"]["total"] = time.time() - t0
        return res
    lang = verdict.language if verdict.language in LANGS else "en"
    t1 = time.time()
    draft, issues, revisions = draft_checked(llm, job, lang, cfg, record=history.recorder(cfg, job, llm))
    res["letter_state"] = "done"
    res["letter"] = {"lang": lang, "subject": draft["subject"], "body": draft["body"],
                     "issues": list(issues), "revisions": revisions}
    res["seconds"]["letter"] = time.time() - t1
    res["seconds"]["total"] = time.time() - t0
    return res


def explain(llm, cfg, job: Job, letter: bool = True) -> str:
    return render_text(analyse(llm, cfg, job, letter))


def render_text(res: dict) -> str:
    out = ([f"MODELS   {res['models']}"] if res["models"] else [])
    out += [f"JOB      {res['title']}" + (f" @ {res['company']}" if res["company"] else ""),
            f"FAMILY   {res['family']}"]
    if res["cv_id"]:
        out.append(f"CV       {res['cv_id']} -> {res['cv_file'] or '?'} ({'found' if res['cv_found'] else 'MISSING FILE'})")
    else:
        out.append("CV       none mapped for this family (would go to human review)")
    out.append(f"SCORE    {res['overall']}    DECISION {res['decision']}")
    out.append("         " + " | ".join(f"{d} {res['dimensions'].get(d) if res['dimensions'].get(d) is not None else '?'}" for d in DIMENSIONS))
    out.append("WHY")
    out += [f"  - {r}" for r in res["reasons"]]
    if res["requirements"]:
        out.append("MANDATORY REQUIREMENTS   (model claim -> what the rules count)")
        for r in res["requirements"]:
            star = "*" if r["disqualifying"] else " "
            out.append(f"  {star} {r['requirement']}: {r['status']} on {r['basis']} -> {r['effective']}")
            if r["evidence"]:
                out.append(f"        quote: \"{r['evidence']}\"")
        out.append("  (* = disqualifying if unmet)")
    arr = res["arrangement"]
    out.append(f"WORK     remote={arr.get('remote', '?')}  eligible={arr.get('eligible', arr.get('portugal_eligible', '?'))}"
               f"  type={arr.get('employment_type', '?')}")
    if arr.get("evidence"):
        out.append(f"         quote: \"{str(arr['evidence'])[:160]}\"")
    out.append(f"LANGUAGE {res['language']}    CONTACT {res['contact'] or 'none in posting (apply via the posting)'}")
    if res["letter_state"] == "off":
        return "\n".join(out)
    if res["letter_state"] == "skipped":
        out.append("LETTER   skipped: rejected jobs get no letter")
        return "\n".join(out)
    L = res["letter"]
    note = f" after {L['revisions']} automatic revision(s)" if L["revisions"] else ""
    out += ["", f"--- LETTER ({L['lang']}) ---", f"Subject: {L['subject']}", "", L["body"], "",
            "VERIFICATION " + (f"ok{note}: would be queued as 'drafted'" if not L["issues"]
                               else f"FAILED{note}: would be queued as 'needs_edit'")]
    out += [f"  ! {i}" for i in L["issues"]]
    return "\n".join(out)
