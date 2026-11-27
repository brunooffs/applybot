from __future__ import annotations

from ..models import Job
from ..text import html_to_text


def parse_job(token: str, account: str, j: dict) -> Job:
    place = ", ".join(p for p in (j.get("city"), j.get("state"), j.get("country")) if p)
    wt = (j.get("workplace_type") or "").lower()
    remote = True if (j.get("telecommuting") or wt == "remote") else (False if wt in ("on_site", "hybrid") else None)
    if remote and "remote" not in place.lower():
        place = f"Remote, {place}" if place else "Remote"
    return Job(
        source="workable",
        ext_id=f"{token}-{j.get('shortcode') or j.get('code')}",
        company=account or token,
        title=j.get("title", ""),
        url=j.get("url") or j.get("shortlink") or "",
        location=place,
        remote=remote,
        description=html_to_text(j.get("description")),
        employment_type=j.get("employment_type") or "",
        posted_at=j.get("published_on") or j.get("created_at") or "",
    )


def fetch(token: str, http):
    # Workable's documented public endpoint (it redirects to apply.workable.com's widget API).
    r = http.get(f"https://www.workable.com/api/accounts/{token}", params={"details": "true"})
    r.raise_for_status()
    data = r.json()
    for j in data.get("jobs", []):
        yield parse_job(token, data.get("name") or "", j)
