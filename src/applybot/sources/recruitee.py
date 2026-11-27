from __future__ import annotations

from ..models import Job
from ..text import html_to_text
from ._geo import with_country


def parse_job(token: str, j: dict) -> Job:
    body = "\n\n".join(p for p in (html_to_text(j.get("description")), html_to_text(j.get("requirements"))) if p)
    if j.get("remote"):
        remote = True
    elif j.get("on_site") or j.get("hybrid"):
        remote = False
    else:
        remote = None
    location = with_country(j.get("location") or "", j.get("country_code"))
    if j.get("remote") and "remote" not in location.lower():
        location = f"Remote, {location}" if location else "Remote"
    return Job(
        source="recruitee",
        ext_id=f"{token}-{j.get('id')}",
        company=j.get("company_name") or token,
        title=j.get("title", ""),
        url=j.get("careers_url") or j.get("url") or "",
        location=location,
        remote=remote,
        description=body,
        employment_type=j.get("employment_type_code") or "",
        posted_at=j.get("published_at") or "",
    )


def fetch(token: str, http):
    r = http.get(f"https://{token}.recruitee.com/api/offers/")
    r.raise_for_status()
    for j in r.json().get("offers", []):
        if j.get("status", "published") == "published":
            yield parse_job(token, j)
