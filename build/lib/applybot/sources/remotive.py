from __future__ import annotations

from ..models import Job
from ..text import html_to_text


def parse_job(j: dict) -> Job:
    return Job(
        source="remotive",
        ext_id=str(j.get("id")),
        company=j.get("company_name", ""),
        title=j.get("title", ""),
        url=j.get("url", ""),
        location=j.get("candidate_required_location", "") or "",
        remote=True,
        description=html_to_text(j.get("description", "")),
        employment_type=j.get("job_type", "") or "",
        posted_at=j.get("publication_date", "") or "",
    )


def fetch(category: str | None, http):
    params = {"category": category} if category else {}
    r = http.get("https://remotive.com/api/remote-jobs", params=params)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield parse_job(j)
