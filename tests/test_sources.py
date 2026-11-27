"""Parser tests use payloads shaped like each vendor's documented API.
They prove the parsers are self-consistent, NOT that the live APIs match:
that only the first real `applybot ingest` can show."""
from applybot.sources import arbeitnow, ashby, greenhouse, lever, recruitee, remotive, workable
from applybot.sources._geo import with_country


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


# Payloads below are copied in shape from live responses (Arbeitnow, Recruitee) and from Workable's
# published API reference (Workable was not reachable from the build machine).

def test_arbeitnow():
    j = arbeitnow.parse_job({
        "slug": "senior-software-engineer-koln-173707", "company_name": "Catapultsports", "title": "Senior Software Engineer",
        "description": "&lt;p&gt;Catapult is building&lt;/p&gt;", "remote": True,
        "url": "https://www.arbeitnow.com/jobs/companies/catapultsports/senior-software-engineer-koln-173707",
        "tags": ["Impect - Engineering"], "job_types": ["full time", "permanent"], "location": "Köln", "created_at": 1795719317})
    assert j.id == "arbeitnow:senior-software-engineer-koln-173707" and j.company == "Catapultsports"
    assert j.location == "Remote, Köln" and j.remote is True and j.description == "Catapult is building"
    assert j.employment_type == "full time, permanent" and j.posted_at.startswith("2026")
    assert arbeitnow.parse_job({"slug": "x", "remote": False, "location": "Münster"}).remote is None


def test_arbeitnow_pages_stop_at_the_last_page_and_at_the_limit():
    import types

    class Http:
        def __init__(self, pages):
            self.pages, self.asked = pages, []

        def get(self, url, params=None):
            n = params["page"]
            self.asked.append(n)
            data = {"data": [{"slug": f"s{n}-{i}"} for i in range(2)],
                    "links": {"next": f"{url}?page={n + 1}" if n < self.pages else None}}
            return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    h = Http(pages=9)
    assert len(list(arbeitnow.fetch(2, h, delay=0))) == 4 and h.asked == [1, 2]
    h = Http(pages=1)
    assert len(list(arbeitnow.fetch(5, h, delay=0))) == 2 and h.asked == [1]


def test_recruitee():
    j = recruitee.parse_job("vandebron", {
        "id": 2821997, "company_name": "Vandebron", "title": "Engineering Team Lead (Backend)", "status": "published",
        "description": "<h4>Who we are</h4><p>At Vandebron</p>", "requirements": "<ul><li>Solid hands-on experience</li></ul>",
        "careers_url": "https://werkenbij.vandebron.nl/o/engineering-team-lead-backend",
        "location": "Amsterdam, Noord-Holland, Nederland", "country_code": "NL", "remote": False, "hybrid": True,
        "on_site": False, "employment_type_code": "fulltime_fixed_term", "published_at": "2026-11-10 09:54:46 UTC"})
    assert j.id == "recruitee:vandebron-2821997" and j.remote is False
    assert j.location == "Amsterdam, Noord-Holland, Nederland, Netherlands"      # English name added for the location check
    assert "Who we are" in j.description and "Solid hands-on experience" in j.description
    assert j.employment_type == "fulltime_fixed_term" and j.url.startswith("https://werkenbij.")
    r = recruitee.parse_job("x", {"id": 1, "remote": True, "location": "", "country_code": "PT"})
    assert r.remote is True and r.location == "Remote, Portugal"


def test_recruitee_skips_unpublished_offers():
    import types
    data = {"offers": [{"id": 1, "status": "published", "title": "A"}, {"id": 2, "status": "draft", "title": "B"}, {"id": 3, "title": "C"}]}
    http = types.SimpleNamespace(get=lambda url: types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: data))
    assert [j.title for j in recruitee.fetch("x", http)] == ["A", "C"]


def test_workable():
    j = workable.parse_job("groove-tech", "Groove Tech", {
        "title": "Finance Manager", "code": "UK/1/FM", "shortcode": "GROOV006", "country": "United Kingdom", "state": "London",
        "city": "London", "department": "Finance", "telecommuting": False, "published_on": "2019-04-07",
        "url": "https://groove-tech.workable.com/jobs/724378555",
        "application_url": "https://groove-tech.workable.com/jobs/724378555/candidates/new",
        "shortlink": "https://groove-tech.workable.com/j/GROOV006", "created_at": "2019-04-07",
        "description": " <p>We are looking for an experienced Finance Manager...</p>", "employment_type": "Full-time"})
    assert j.id == "workable:groove-tech-GROOV006" and j.company == "Groove Tech"
    assert j.location == "London, London, United Kingdom" and j.remote is None
    assert j.url.endswith("/jobs/724378555") and j.description.startswith("We are looking")
    r = workable.parse_job("t", "", {"shortcode": "A", "telecommuting": True, "country": "Portugal"})
    assert r.remote is True and r.location == "Remote, Portugal" and r.company == "t"


def test_country_names_are_added_only_when_missing():
    assert with_country("Berlin, Germany", "DE") == "Berlin, Germany"
    assert with_country("", "pt") == "Portugal" and with_country("Lisboa", None) == "Lisboa"
    assert with_country("Somewhere", "ZZ") == "Somewhere"


def test_new_parsers_tolerate_missing_fields():
    assert arbeitnow.parse_job({}).title == "" and recruitee.parse_job("a", {}).remote is None
    assert workable.parse_job("a", "", {}).location == ""


def test_iter_jobs_reaches_every_configured_source(cfg, monkeypatch):
    import httpx

    from applybot import sources as src

    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url.split("?")[0])
        if "workable.com/api/accounts/acme" in url:
            return httpx.Response(200, json={"name": "Acme", "jobs": [{"title": "W", "shortcode": "S1"}]})
        if "acme.recruitee.com" in url:
            return httpx.Response(200, json={"offers": [{"id": 5, "title": "R", "status": "published"}]})
        if "arbeitnow.com" in url:
            return httpx.Response(200, json={"data": [{"slug": "a1", "title": "A"}], "links": {"next": None}})
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(src.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(src.time, "sleep", lambda s: None)
    c = type("C", (), {})()
    c.settings = cfg.settings
    c.sources = {"workable": ["acme"], "recruitee": ["acme"], "arbeitnow": {"enabled": True, "pages": 3}}
    errors = []
    jobs = list(src.iter_jobs(c, on_error=lambda *a: errors.append(a)))
    assert sorted(j.id for j in jobs) == ["arbeitnow:a1", "recruitee:acme-5", "workable:acme-S1"] and not errors
    c.sources = {"recruitee": ["gone"], "arbeitnow": {"enabled": False}}
    errors.clear()
    assert list(src.iter_jobs(c, on_error=lambda *a: errors.append(a))) == [] and len(errors) == 1   # 404: reported, run continues


def test_check_source_counts_the_new_kinds():
    import types

    from applybot import runner

    def http(payload):
        return types.SimpleNamespace(get=lambda url: types.SimpleNamespace(status_code=200, json=lambda: payload))

    assert runner.check_source("recruitee", "acme", http({"offers": [1, 2]})) == (True, "recruitee/acme: OK, 2 postings listed")
    assert runner.check_source("workable", "acme", http({"jobs": [1]}))[1].endswith("1 posting listed")
    assert runner.check_source("arbeitnow", "all", http({"data": [1, 2, 3]}))[1].endswith("3 postings listed")
