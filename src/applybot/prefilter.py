"""Cheap keyword gate that runs before any LLM call."""
from __future__ import annotations

import re

from .models import Job


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
