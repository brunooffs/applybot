"""Arbeitnow: a free public job board API for Germany and Europe, no key and no company list.

It asks to be used politely and linked back to (https://www.arbeitnow.com); commercial use needs
their agreement. Newest first, 300 jobs a page, so `pages` bounds what one run downloads."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..models import Job
from ..text import html_to_text


def parse_job(j: dict) -> Job:
    created = j.get("created_at")
    posted = (datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="seconds")
              if isinstance(created, (int, float)) else "")
    location = j.get("location") or ""
    remote = bool(j.get("remote"))
    if remote and "remote" not in location.lower():
        location = f"Remote, {location}" if location else "Remote"
    return Job(
        source="arbeitnow",
        ext_id=str(j.get("slug")),
        company=j.get("company_name", ""),
        title=j.get("title", ""),
        url=j.get("url", ""),
        location=location,
        remote=True if remote else None,
        description=html_to_text(j.get("description")),
        employment_type=", ".join(j.get("job_types") or []),
        posted_at=posted,
    )


def fetch(pages: int, http, delay: float = 1.0):
    for page in range(1, max(1, int(pages)) + 1):
        r = http.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") or []
        for j in rows:
            yield parse_job(j)
        if not rows or not (data.get("links") or {}).get("next"):
            break
        time.sleep(delay)
