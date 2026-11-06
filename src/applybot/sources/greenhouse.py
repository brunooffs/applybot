from __future__ import annotations

from ..models import Job
from ..text import html_to_text


def parse_job(token: str, j: dict) -> Job:
    loc = (j.get("location") or {}).get("name", "")
    return Job(
        source="greenhouse",
        ext_id=f"{token}-{j.get('id')}",
        company=j.get("company_name") or token,
        title=j.get("title", ""),
        url=j.get("absolute_url", ""),
        location=loc,
        description=html_to_text(j.get("content", "")),
        posted_at=j.get("updated_at", ""),
    )


def fetch(token: str, http):
    r = http.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield parse_job(token, j)
