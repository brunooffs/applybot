from __future__ import annotations

from ..models import Job


def parse_job(token: str, j: dict) -> Job:
    return Job(
        source="ashby",
        ext_id=f"{token}-{j.get('id')}",
        company=token,
        title=j.get("title", ""),
        url=j.get("jobUrl", ""),
        location=j.get("location", "") or "",
        remote=j.get("isRemote"),
        description=j.get("descriptionPlain", "") or "",
        employment_type=j.get("employmentType", "") or "",
        posted_at=j.get("publishedAt", "") or "",
    )


def fetch(token: str, http):
    r = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield parse_job(token, j)
