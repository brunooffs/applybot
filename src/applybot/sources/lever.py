from __future__ import annotations

from datetime import datetime, timezone

from ..models import Job


def parse_job(token: str, j: dict) -> Job:
    cats = j.get("categories") or {}
    wp = (j.get("workplaceType") or "").lower()
    remote = True if wp == "remote" else (False if wp in ("on-site", "onsite") else None)
    created = j.get("createdAt")
    posted = ""
    if isinstance(created, (int, float)):
        posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    return Job(
        source="lever",
        ext_id=f"{token}-{j.get('id')}",
        company=token,
        title=j.get("text", ""),
        url=j.get("hostedUrl", ""),
        location=cats.get("location", "") or "",
        remote=remote,
        description=j.get("descriptionPlain", "") or "",
        employment_type=cats.get("commitment", "") or "",
        posted_at=posted,
    )


def fetch(token: str, http):
    r = http.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    r.raise_for_status()
    for j in r.json():
        yield parse_job(token, j)
