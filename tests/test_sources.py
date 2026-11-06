"""Parser tests use payloads shaped like each vendor's documented API.
They prove the parsers are self-consistent, NOT that the live APIs match:
that only the first real `applybot ingest` can show."""
from applybot.sources import ashby, greenhouse, lever, remotive


def test_greenhouse():
    j = greenhouse.parse_job("acme", {
        "id": 42, "title": "Platform Engineer", "absolute_url": "https://x/42",
        "location": {"name": "Lisbon"}, "company_name": "Acme",
        "content": "&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;&lt;/p&gt;", "updated_at": "2026-10-01"})
    assert j.id == "greenhouse:acme-42" and j.company == "Acme"
    assert j.description == "Build things"


def test_lever():
    j = lever.parse_job("acme", {
        "id": "abc", "text": "Backend Engineer", "hostedUrl": "https://x/abc",
        "categories": {"location": "Remote", "commitment": "Full-time"},
        "descriptionPlain": "Do stuff", "workplaceType": "remote", "createdAt": 1760000000000})
    assert j.remote is True and j.employment_type == "Full-time" and j.posted_at.startswith("2025")


def test_ashby():
    j = ashby.parse_job("acme", {"id": "i", "title": "SRE", "jobUrl": "u", "isRemote": True,
                                 "descriptionPlain": "d", "employmentType": "FullTime"})
    assert j.remote is True and j.id == "ashby:acme-i"


def test_remotive():
    j = remotive.parse_job({"id": 7, "title": "Cloud Architect", "company_name": "Z", "url": "u",
                            "candidate_required_location": "Europe", "job_type": "full_time",
                            "description": "<p>hi</p>", "publication_date": "2026-10-01"})
    assert j.location == "Europe" and j.description == "hi" and j.remote is True


def test_parsers_tolerate_missing_fields():
    assert greenhouse.parse_job("a", {}).title == ""
    assert lever.parse_job("a", {}).remote is None
    assert ashby.parse_job("a", {}).location == ""
    assert remotive.parse_job({}).company == ""
