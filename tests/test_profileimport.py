import http.client
import shutil
import time
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest
import yaml

from applybot import profileimport as pi
from applybot import web
from applybot.config import DEFAULTS, load_config
from applybot.factsimport import ImportProblem
from test_factsimport import CV
from test_web import env, req  # noqa: F401  (fixtures and helpers shared with the web tests)

LETTER = ("Dear team, I am looking for remote or hybrid roles where I can build backend platforms. "
          "I am available immediately and based in Portugal. " * 2)


def it(kind, value, quote, field=""):
    return {"kind": kind, "value": value, "quote": quote, **({"field": field} if field else {})}


GOOD = [
    it("name", "Sam Example", "Sam Example"),
    it("headline", "Senior Backend Engineer", "Initech Payments, Senior Backend Engineer, 2018 to 2024"),
    it("seniority", "Senior", "Initech Payments, Senior Backend Engineer, 2018 to 2024"),
    it("experience_years", "15+", "15+ years of experience"),
    it("home_base", "Lisbon, Portugal", "Lisbon, Portugal"),
    it("language", "English", "English: C1"),
    it("positioning", "Backend engineer with 15+ years of experience in payments and logistics.",
       "Backend engineer with 15+ years of experience in payments and logistics."),
    it("capability", "Event-driven systems", "Built an event-driven settlement service"),
    it("family", "Senior Backend Engineer", "Initech Payments, Senior Backend Engineer, 2018 to 2024"),
    it("family", "Platform Engineer", "Built an event-driven settlement service"),
    it("preference", "remote or hybrid", "remote or hybrid roles", "working_mode"),
    it("preference", "immediately", "available immediately", "availability"),
]
BAD = [
    it("positioning", "Backend engineer with 99 years of experience.", "Backend engineer with 15+ years of experience"),  # number not in text
    it("capability", "Kubernetes operations", "Kubernetes and Terraform"),          # quote not in text
    it("experience_years", "25+", "15+ years of experience"),                      # anchored value not in text
    it("preference", "x", "remote or hybrid roles", "salary"),                      # unknown preference field
    {"kind": "nonsense", "value": "x", "quote": "x"}, "not a dict",
]


class Fake:
    def __init__(self, items):
        self.items = items

    def complete_json(self, system, user):
        assert "TEXT:" in user and "second document" in system.lower()
        return {"items": self.items}


# --- checks -----------------------------------------------------------------------------

def test_verify_keeps_only_what_the_text_supports():
    text = pi.combine(CV, LETTER)
    kept, dropped = pi.verify(pi.sanitize({"items": GOOD + BAD}), text)
    assert dropped == 3 and len(kept) == len(GOOD)
    assert not any("99 years" in p["value"] or p["value"] == "25+" for p in kept)


def test_single_values_keep_the_first_and_lists_dedupe():
    twice = GOOD + [it("headline", "Principal Engineer", "Initech Payments, Senior Backend Engineer, 2018 to 2024"),
                    it("capability", "event-driven SYSTEMS", "Built an event-driven settlement service")]
    kept, _ = pi.verify(pi.sanitize({"items": twice}), pi.combine(CV, LETTER))
    assert [p["value"] for p in kept if p["kind"] == "headline"] == ["Senior Backend Engineer"]
    assert sum(p["kind"] == "capability" for p in kept) == 1


def test_extract_marks_titles_the_text_uses_as_primary():
    props, _ = pi.extract(Fake(GOOD), pi.combine(CV, LETTER))
    tiers = {p["value"]: p["tier"] for p in props if p["kind"] == "family"}
    assert tiers == {"Senior Backend Engineer": "primary", "Platform Engineer": ""}


def test_suggest_keywords_drops_seniority_and_generic_heads():
    kw = pi.suggest_keywords(["Senior Backend Engineer", "Solutions Architect", "Scrum Master", "Principal Engineer"])
    assert kw == ["backend engineer", "backend", "solutions architect", "scrum master", "scrum"]   # "engineer" alone is too broad
    assert pi.suggest_keywords(["Senior Talent Acquisition Partner", "Executive Recruiter", "Recruitment Consultant"]) == [
        "talent acquisition partner", "talent", "executive recruiter", "recruitment consultant", "recruitment"]
    assert pi.clean_keywords("  Backend Engineer \nbackend engineer\n\nx\n") == ["backend engineer"]


# --- building and writing ---------------------------------------------------------------

ANSWERS = {"name": "Sam Example", "headline": "Senior Backend Engineer", "home_country": "Portugal",
           "authorised_regions": ["EU", "nonsense"], "languages": ["Portuguese", "English"],
           "families": {"primary": ["Senior Backend Engineer"], "secondary": [], "selective": {"Engineering Manager": ""}},
           "capabilities": ["Event-driven systems"], "positioning": "Backend engineer: payments."}


def test_build_profile_needs_home_country_and_a_primary_family():
    with pytest.raises(ImportProblem, match="home country"):
        pi.build_profile({**ANSWERS, "home_country": " "})
    with pytest.raises(ImportProblem, match="primary"):
        pi.build_profile({**ANSWERS, "families": {"primary": [], "secondary": ["X"], "selective": {}}})
    with pytest.raises(ImportProblem, match="do not recognise"):
        pi.build_profile({**ANSWERS, "home_country": "Lisbon, Portugal"})
    assert pi.guess_country("Lisbon, Portugal") == "Portugal" and pi.guess_country("Remote") == ""
    p = pi.build_profile(ANSWERS)
    assert p["work_eligibility"] == {"home_country": "Portugal", "authorised_regions": ["Portugal", "EU"],
                                     "accepts_relocation": False}
    assert p["seniority"] == "TODO" and p["preferences"]["working_mode"] == ["remote", "hybrid"]   # unanswered: marked / defaults
    assert p["families"]["selective"] == {"Engineering Manager": "Partial match: always reviewed by you"}


def make_home(tmp_path):
    home = tmp_path / "h"
    for rel in ("config", "cvs", "data"):
        (home / rel).mkdir(parents=True)
    for f in (DEFAULTS / "config").iterdir():
        shutil.copy(f, home / "config" / f.name.removesuffix(".example"))
    shutil.copy(DEFAULTS / "cvs" / "cvs.yaml.example", home / "cvs" / "cvs.yaml")
    return home


def test_profile_is_written_only_over_the_fictional_example(tmp_path):
    home = make_home(tmp_path)
    cfg = load_config(home)
    profile = pi.build_profile(ANSWERS)
    assert pi.apply_profile(cfg, profile).name == "profile.yaml"
    assert yaml.safe_load((home / "config" / "profile.yaml").read_text())["name"] == "Sam Example"
    assert "Alex Example" in (home / "config" / "profile.yaml.bak").read_text()
    # now it is a real profile: the next import must not touch it
    mine = (home / "config" / "profile.yaml").read_text()
    cfg = load_config(home)
    assert pi.apply_profile(cfg, profile).name == "profile.suggested.yaml"
    assert (home / "config" / "profile.yaml").read_text() == mine


def crit(home):
    return yaml.safe_load((home / "config" / "criteria.yaml").read_text())


def test_starter_lists_are_replaced_and_clashing_skip_words_are_dropped(tmp_path):
    home = make_home(tmp_path)
    cfg = load_config(home)
    before = crit(home)
    assert pi.criteria_is_starter(cfg)
    out = pi.apply_criteria(cfg, ["talent acquisition partner", "Recruiter", "recruitment"])
    after = crit(home)
    assert out == {"mode": "replaced", "added": 3, "removed": ["recruiter"], "conflicts": []}
    assert after["prefilter"]["include_title_keywords"] == ["talent acquisition partner", "recruiter", "recruitment"]
    assert "recruiter" not in after["prefilter"]["exclude_title_keywords"]
    assert after["prefilter"]["exclude_title_keywords"] == [k for k in before["prefilter"]["exclude_title_keywords"] if k != "recruiter"]
    assert {k: v for k, v in after.items() if k != "prefilter"} == {k: v for k, v in before.items() if k != "prefilter"}
    assert "weights:" in (home / "config" / "criteria.yaml").read_text() and "# whole-word match" in (home / "config" / "criteria.yaml").read_text()
    assert not pi.criteria_is_starter(load_config(home))
    again = pi.apply_criteria(load_config(home), ["talent acquisition partner", "recruiter", "recruitment"])
    assert again["mode"] == "appended" and again["added"] == 0


def test_a_recruiter_profile_can_find_recruiter_jobs(tmp_path):
    from applybot.models import Job
    from applybot.prefilter import prefilter
    home = make_home(tmp_path)
    kws = pi.suggest_keywords(["Senior Talent Acquisition Partner", "Senior Recruiter", "Technical Recruiter"])
    pi.apply_criteria(load_config(home), kws)
    crit_cfg = load_config(home).criteria
    job = lambda t: Job(source="x", ext_id=t, company="c", title=t, url="u", location="Lisbon")   # noqa: E731
    assert prefilter(job("Senior Recruiter"), crit_cfg)[0]
    assert prefilter(job("Talent Acquisition Partner"), crit_cfg)[0]
    assert not prefilter(job("Junior Recruiter"), crit_cfg)[0]            # the skip words that remain still apply
    assert not prefilter(job("Backend Engineer"), crit_cfg)[0]            # the engineering starter keywords are gone


def test_your_own_lists_only_get_keywords_appended(tmp_path):
    home = make_home(tmp_path)
    (home / "config" / "criteria.yaml").write_text(
        "prefilter:\n  include_title_keywords:\n    - backend   # mine\n    # a note\n    - go\n"
        "  exclude_title_keywords:\n    - junior\n    - recruiter\nweights:\n  technical: 30\n")
    cfg = load_config(home)
    assert not pi.criteria_is_starter(cfg)
    out = pi.apply_criteria(cfg, ["recruiter", "Backend", "quantum gardener"])
    text = (home / "config" / "criteria.yaml").read_text()
    assert out == {"mode": "appended", "added": 2, "removed": [], "conflicts": ["recruiter"]}   # warned, not removed
    assert crit(home)["prefilter"]["include_title_keywords"] == ["backend", "go", "recruiter", "quantum gardener"]
    assert crit(home)["prefilter"]["exclude_title_keywords"] == ["junior", "recruiter"]
    assert "# mine" in text and "# a note" in text


def test_an_empty_skip_list_stays_valid(tmp_path):
    home = make_home(tmp_path)
    pi.apply_criteria(load_config(home), ["recruiter"], exclude=[])
    c = crit(home)
    assert c["prefilter"]["exclude_title_keywords"] == [] and "# whole-word match" in (home / "config" / "criteria.yaml").read_text()


def test_keywords_work_without_a_criteria_file_of_your_own(tmp_path):
    home = make_home(tmp_path)
    (home / "config" / "criteria.yaml").unlink()
    assert pi.apply_criteria(load_config(home), ["quantum gardener"])["added"] == 1
    assert crit(home)["prefilter"]["include_title_keywords"] == ["quantum gardener"]


# --- the page, driven the way a browser drives it ---------------------------------------

def upload(e, files):
    boundary = "----applybotProfile"
    body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"_t\"\r\n\r\n{e.token}\r\n".encode()
    for field, (fname, data) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    c = http.client.HTTPConnection("127.0.0.1", e.port)
    c.request("POST", "/profile/upload", body=body, headers={
        "Host": f"127.0.0.1:{e.port}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    r = c.getresponse()
    r.read()
    loc = r.getheader("Location")
    c.close()
    return r.status, loc


def form_fields(page, action):
    """What a browser would submit for the form with this action: [(name, value)], defaults as rendered."""
    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields, self.inside, self.sel, self.ta = [], False, None, None

        def handle_starttag(self, tag, a):
            a = dict(a)
            if tag == "form":
                self.inside = a.get("action") == action
            elif not self.inside:
                return
            elif tag == "input" and a.get("name"):
                if a.get("type") in ("checkbox", "radio"):
                    if "checked" in a:
                        self.fields.append((a["name"], a.get("value", "on")))
                else:
                    self.fields.append((a["name"], a.get("value", "")))
            elif tag == "textarea":
                self.ta = [a["name"], ""]
            elif tag == "select":
                self.sel = [a["name"], None, None]
            elif tag == "option" and self.sel is not None:
                if self.sel[2] is None:
                    self.sel[2] = a.get("value", "")
                if "selected" in a:
                    self.sel[1] = a.get("value", "")

        def handle_data(self, data):
            if self.ta is not None:
                self.ta[1] += data

        def handle_endtag(self, tag):
            if tag == "textarea" and self.ta is not None:
                self.fields.append(tuple(self.ta))
                self.ta = None
            elif tag == "select" and self.sel is not None:
                self.fields.append((self.sel[0], self.sel[1] if self.sel[1] is not None else self.sel[2]))
                self.sel = None
            elif tag == "form":
                self.inside = False

    p = P()
    p.feed(page)
    return p.fields


def post_pairs(e, path, pairs):
    c = http.client.HTTPConnection("127.0.0.1", e.port)
    c.request("POST", path, body=urlencode([("_t", e.token)] + [x for x in pairs if x[0] != "_t"]),
              headers={"Host": f"127.0.0.1:{e.port}", "Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    r.read()
    loc = r.getheader("Location")
    c.close()
    return r.status, loc


def wait_done():
    for _ in range(100):
        if pi.CURRENT is None or pi.CURRENT.state != "running":
            return pi.CURRENT
        time.sleep(0.05)


def test_profile_page_upload_review_and_save(env, monkeypatch):  # noqa: F811
    pi.CURRENT = None
    shutil.copy(DEFAULTS / "cvs" / "cvs.yaml.example", env.home / "cvs" / "cvs.yaml")   # the starter list
    monkeypatch.setattr(web, "make_llm", lambda cfg: Fake(GOOD + BAD))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-real")
    page = req(env, "GET", "/profile")[1]
    assert "Read my documents" in page and "fictional example" in page and "Profile</a>" in page
    code, loc = upload(env, {"cv": ("cv.txt", CV.encode()), "extra": ("letter.txt", LETTER.encode())})
    assert code == 303 and "ok=" in loc
    assert wait_done().state == "done"
    page = req(env, "GET", "/profile")[1]
    assert "letter.txt" in page and "remote or hybrid roles" in page and "99 years" not in page
    pairs = form_fields(page, "/profile/save")
    names = [n for n, _ in pairs]
    assert "f__home_country" in names and "f__keywords" in names and any(n.endswith("__tier") for n in names)
    # the person's choices on top of what the page rendered
    pairs = [(n, v) for n, v in pairs if n not in ("f__home_country", "f__relocation")]
    assert ("f__home_country", "Portugal") in form_fields(page, "/profile/save")   # guessed from "Lisbon, Portugal"
    pairs += [("f__home_country", "Portugal"), ("f__regions", "EU")]
    code, loc = post_pairs(env, "/profile/save", pairs)
    assert code == 303 and "ok=" in loc, loc
    prof = yaml.safe_load((env.home / "config" / "profile.yaml").read_text())
    assert prof["name"] == "Sam Example" and prof["headline"] == "Senior Backend Engineer"
    assert prof["work_eligibility"]["authorised_regions"] == ["Portugal", "EU"]
    assert prof["families"]["primary"] == ["Senior Backend Engineer"]      # Platform Engineer started left out
    assert prof["preferences"]["working_mode"] == ["remote", "hybrid"] and prof["preferences"]["availability"] == "immediately"
    assert prof["languages"] == ["English"] and prof["capabilities"] == ["Event-driven systems"]
    assert load_config(env.home).family_to_cv == {"Senior Backend Engineer": "cv"}     # the uploaded cv.txt took over the starter entry
    assert (env.home / "cvs" / "cv.txt").read_text() == CV and (env.home / "cvs" / "cover-letters" / "letter.txt").exists()
    kws = yaml.safe_load((env.home / "config" / "criteria.yaml").read_text())["prefilter"]["include_title_keywords"]
    assert kws == ["backend engineer", "backend"]     # the starter engineering list was replaced
    assert pi.load_state(load_config(env.home)) is None


def test_profile_save_without_import_or_answers(env):  # noqa: F811
    pi.discard_state(load_config(env.home))
    assert "no+pending" in req(env, "POST", "/profile/save", {})[2].lower().replace("%20", "+")
    pi.CURRENT = None
