"""Cheap keyword gate that runs before any LLM call."""
from __future__ import annotations

import re

from .models import Job

SENIOR_WORDS = ("senior", "staff", "principal", "lead", "architect", "head of")
DEFAULT_BOOST = ["backend", "back-end", "architect", "distributed", "platform", "cloud", "aws", "node", "python"]


def relevance(job: Job, criteria: dict, work_eligibility: dict | None = None) -> int:
    """Cheap priority for scoring order, no LLM: higher = score sooner. Title seniority and core keywords,
    plus the location check (a place outside your authorised regions goes last). Optional
    `ranking.boost_title_keywords` in criteria.yaml replaces the default keyword list."""
    from .eligibility import location_eligibility
    title = job.title.lower()
    boost = [k.lower() for k in (criteria.get("ranking", {}).get("boost_title_keywords") or DEFAULT_BOOST)]
    pts = 3 if any(w in title for w in SENIOR_WORDS) else 0
    pts += 2 * min(2, sum(1 for k in boost if k in title))
    loc, _ = location_eligibility(job.location, job.description, work_eligibility)
    return pts + {"yes": 3, "no": -10}.get(loc, 0)


def prefilter(job: Job, criteria: dict) -> tuple[bool, str]:
    pf = criteria.get("prefilter", {})
    title = job.title.lower()
    for kw in pf.get("exclude_title_keywords", []):
        if re.search(rf"\b{re.escape(kw.lower())}\b", title):
            return False, f"excluded title keyword: {kw}"
    include = [k.lower() for k in pf.get("include_title_keywords", [])]
    if include and not any(k in title for k in include):
        return False, "no target keyword in title"
    return True, ""
