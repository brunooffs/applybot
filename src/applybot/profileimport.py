"""Profile, CV list and title keywords from a CV (and an optional cover letter).

Same rule as the facts import: the model proposes, plain code checks, you decide.
  1. The CV, and optionally a cover letter or profile text, are read locally (factsimport.read_document).
  2. The model proposes headline, seniority, job families, capabilities, positioning and the
     preferences the text states, each with the line it came from. A proposal is dropped unless that
     line occurs in the text, and any number it contains occurs there too.
  3. What a text cannot tell (where you may legally work, what you want) is asked on the page
     as a short questionnaire. Work eligibility is never guessed: it decides rejects.
  4. Only what you confirm is written:
       config/profile.yaml   a fresh one while it is still the fictional example (old kept as .bak);
                             an existing real profile is never touched: the result goes to
                             config/profile.suggested.yaml to compare by hand
       cvs/cvs.yaml          the starter list (one CV "general") gets your families; a list you
                             edited is left alone and the families it does not cover are reported
       config/criteria.yaml  title keywords: the starter lists (written for a backend-engineering search)
                             are replaced; lists you edited only get new keywords appended
Every file written is parsed again and must equal what was confirmed, or nothing is written.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path

import yaml

from . import factsimport as fi
from .factsimport import ImportProblem

SINGLE = ("name", "headline", "seniority", "experience_years", "home_base", "positioning")
KINDS = ("name", "headline", "seniority", "experience_years", "home_base", "language", "positioning",
         "capability", "family", "preference")
ANCHORED = ("name", "experience_years", "home_base", "language")   # the value itself must occur in the text
PREF_FIELDS = ("working_mode", "employment_types", "availability", "relocation")
MAX_LEN = {"headline": 100, "seniority": 40, "capability": 120, "family": 80, "positioning": 700, "name": 80,
           "experience_years": 30, "home_base": 80, "language": 40}
REGIONS = ("EU", "EEA", "Europe", "EMEA", "North America", "LATAM", "APAC", "worldwide")
MODES = ("remote", "hybrid", "onsite")
TYPES = ("permanent", "b2b", "contract", "freelance")
TIERS = ("primary", "secondary", "selective")
SENIORITY_WORDS = {"senior", "sr", "sr.", "lead", "principal", "staff", "junior", "jr", "jr."}
GENERIC_HEADS = {"engineer", "developer", "manager", "lead", "software", "technical", "senior", "principal",
                 "staff", "solutions", "systems", "full", "agile", "project", "delivery", "ai", "infrastructure",
                 "executive", "associate", "assistant", "head", "chief", "global", "specialist", "consultant", "analyst"}

SYSTEM = """You read a person's CV, and sometimes a second document (a cover letter or profile text), and propose
what goes into a job-matching profile so they can review it. Output JSON only.

Return {"items": [ ... ]}. Each item has "kind", "value" and "quote":
- name: the person's full name
- headline: a short professional headline (for example "Senior Backend Engineer"), based on how the CV describes the person
- seniority: one word or phrase (Junior, Mid, Senior, Lead, Principal) only if the text supports it
- experience_years: total professional experience exactly as the text states it (for example "20+"); only if stated
- home_base: where the person is based, as written
- language: one language the person speaks (as written)
- positioning: two or three plain sentences describing the person's professional focus, using only what the text says
- capability: one area of capability (for example "Cloud-native and distributed systems"), 6 to 14 items
- family: a job title this person could be hired as, the way job ads name it (for example "Platform Engineer"), 4 to 12 items, most likely first
- preference: add "field" (one of working_mode, employment_types, availability, relocation) and put what the text says in "value"; only if the text states it

Rules:
- "quote" is a short exact span (under 200 characters) copied character for character from the text, from the line the item comes from. Never invent or paraphrase it.
- Use only what the text says. Do not add numbers, employers, technologies or years that it does not contain.
- A second document (for example a cover letter) may be used for positioning, preferences and target roles. Never treat it as proof of employment facts.
- Do not infer where the person may legally work.
- Skip anything you are unsure about.
"""


# --- 1. proposals -------------------------------------------------------------------------

def combine(cv_text: str, extra_text: str = "") -> str:
    """One text for the model and for the checks. The second document is clearly separated."""
    if not extra_text.strip():
        return cv_text
    return cv_text + "\n\n=== SECOND DOCUMENT (cover letter or profile text) ===\n\n" + extra_text


def sanitize(raw) -> list[dict]:
    """Model output of any shape -> well-formed proposals {kind, value, field, quote}. Never raises."""
    items = raw.get("items") if isinstance(raw, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            continue
        kind = it["kind"]
        value = fi._clean_str(it.get("value"))[: MAX_LEN.get(kind, 200)]
        field = fi._clean_str(it.get("field"))
        if kind == "preference":
            if field not in PREF_FIELDS:
                continue
        else:
            field = ""
        if not value:
            continue
        out.append({"kind": kind, "field": field, "value": value, "quote": fi._clean_str(it.get("quote"))})
    return out


def _numbers_ok(value: str, text_norm: str) -> bool:
    """Every number in the value occurs in the text as a number of its own ('30' is not found inside '2030')."""
    return all(re.search(r"(?<![\d.,])" + re.escape(re.sub(r"[.,]+$", "", n)) + r"(?!\d)", text_norm)
               for n in re.findall(r"\d[\d.,+]*", value))


def verify(proposals: list[dict], text: str) -> tuple[list[dict], int]:
    """Keep proposals whose quote is in the text, whose anchored value is in the text, and whose numbers are."""
    tn, td = fi._norm(text), fi._digits(text)
    kept, dropped, seen = [], 0, set()
    for p in proposals:
        q = fi._norm(p["quote"])
        bad = (len(q) < 4 or q not in tn or not _numbers_ok(p["value"], tn)
               or (p["kind"] in ANCHORED and not fi.appears(p["value"], tn, td)))
        key = (p["kind"], p["field"], fi._norm(p["value"])) if p["kind"] not in SINGLE else (p["kind"],)
        if bad:
            dropped += 1
        elif key not in seen:   # the same thing found twice (two chunks) counts once; singles keep the first
            seen.add(key)
            kept.append(p)
    return kept, dropped


def extract(client, text: str, progress=None) -> tuple[list[dict], int]:
    found, dropped = [], 0
    parts = fi._chunks(text)
    for i, chunk in enumerate(parts):
        if progress:
            progress(i, len(parts))
        extra = {"max_tokens": 8000} if hasattr(client, "stage") else {}
        raw = client.complete_json(SYSTEM, "TEXT:\n" + chunk, **extra)
        found += sanitize(raw)
    kept, dropped = verify(found, text)
    for n, p in enumerate(kept, 1):
        p["id"] = f"p{n}"
        if p["kind"] == "family":   # a title the text itself uses is a safe starting point; the rest starts left out
            p["tier"] = "primary" if fi.appears(p["value"], fi._norm(text)) else ""
    return kept, dropped


# --- 2. derived suggestions ---------------------------------------------------------------

def suggest_keywords(families: list[str]) -> list[str]:
    """Title keywords for the cheap pre-filter, from the families: 'Senior Backend Engineer' ->
    'backend engineer' and 'backend'. Editable on the page; only ever appended to criteria.yaml."""
    out: list[str] = []
    for fam in families:
        words = [w for w in re.split(r"\s+", fam.lower().strip()) if w]
        while words and words[0] in SENIORITY_WORDS:
            words.pop(0)
        if not words or (len(words) == 1 and words[0] in GENERIC_HEADS):   # "engineer" alone would match every job
            continue
        for kw in (" ".join(words), words[0] if len(words) > 1 and words[0] not in GENERIC_HEADS else ""):
            if kw and kw not in out and 2 <= len(kw) <= 40:
                out.append(kw)
    return out


def clean_keywords(text) -> list[str]:
    raw = text.splitlines() if isinstance(text, str) else list(text or [])
    out: list[str] = []
    for k in raw:
        k = re.sub(r"\s+", " ", str(k)).strip().lower()
        if 2 <= len(k) <= 40 and k not in out:
            out.append(k)
    return out


def facts_hint(facts: dict) -> dict:
    """What confirmed facts already say, to prefill the page (never the other way round)."""
    if not facts or fi.is_template(facts):
        return {}
    ident = facts.get("identity") or {}
    langs = list(ident.get("languages") or []) or list((facts.get("language_levels") or {}).keys())
    return {"name": ident.get("name") or "", "location": ident.get("location") or "", "languages": [str(x) for x in langs]}


# --- 3. building and writing the profile --------------------------------------------------

def is_starter_profile(profile: dict) -> bool:
    """True when profile.yaml is missing, empty or still the fictional example."""
    return not profile or profile.get("name") == "Alex Example"


def guess_country(text: str) -> str:
    """'Lisbon, Portugal' -> 'Portugal' when the last part is a country applybot knows; else ''."""
    from .eligibility import ALIASES, COUNTRIES
    for part in reversed([x.strip() for x in re.split(r"[,|/]", text or "") if x.strip()]):
        key = ALIASES.get(part.lower(), part.lower())
        if key in COUNTRIES:
            return key.title()
    return ""


def build_profile(a: dict) -> dict:
    """The answers confirmed on the page -> the profile dict. Raises ImportProblem if something needed is missing."""
    home = fi._clean_str(a.get("home_country"))
    if not home:
        raise ImportProblem("Tell me your home country. It decides which postings are rejected for location.")
    from .eligibility import _resolve
    if not _resolve(home):
        raise ImportProblem(f"I do not recognise '{home}' as a country. Write the country name only, for example "
                            "Portugal, so the location rules can work.")
    fam = a.get("families") or {}
    primary = fi._clean_list(fam.get("primary"))
    if not primary:
        raise ImportProblem("Pick at least one primary job family.")
    selective = {fi._clean_str(k): fi._clean_str(v) or "Partial match: always reviewed by you"
                 for k, v in (fam.get("selective") or {}).items() if fi._clean_str(k)}
    todo = lambda v: fi._clean_str(v) or "TODO"   # noqa: E731
    modes = [m for m in a.get("working_mode") or [] if m in MODES] or ["remote", "hybrid"]
    types = [t for t in a.get("employment_types") or [] if t in TYPES] or ["permanent", "contract"]
    regions = [r for r in a.get("authorised_regions") or [] if r in REGIONS]
    relocation = bool(a.get("accepts_relocation"))
    prefs = {
        "geography": fi._clean_str(a.get("geography")) or "Where I am authorised to work",
        "working_mode": modes,
        "relocation": "open to discussion" if relocation else "not planned",
        "employment_types": types,
        "availability": fi._clean_str(a.get("availability")) or "TODO",
    }
    p = {
        "name": todo(a.get("name")),
        "headline": todo(a.get("headline")),
        "seniority": todo(a.get("seniority")),
        "experience_years": todo(a.get("experience_years")),
        "home_base": fi._clean_str(a.get("home_base")) or home,
        "work_eligibility": {"home_country": home, "authorised_regions": [home] + regions,
                             "accepts_relocation": relocation},
        "languages": fi._clean_list(a.get("languages")),
        "positioning": todo(a.get("positioning")),
        "families": {"primary": primary, "secondary": fi._clean_list(fam.get("secondary")), "selective": selective},
        "preferences": prefs,
        "capabilities": fi._clean_list(a.get("capabilities")),
    }
    return p


_HEADER = """# Canonical matching profile (source of truth for scoring).
# Created from your CV by the Profile page. Lines marked TODO need your answer.
# Check "work_eligibility": it decides which postings are rejected because of where they are.
"""
_ELIGIBILITY_NOTE = """# Where you can legally work. Used to reject, or send to review, postings limited to other
# places ("Remote US", "must be based in Canada"). Regions understood: EU, EEA, Europe, EMEA,
# North America, LATAM, APAC, worldwide. Or list countries by name.
"""
_FAMILIES_NOTE = """# Job families used for classification. primary / secondary are matched normally;
# "selective" always goes to human review, with the reason you gave. Each family needs a CV in cvs/cvs.yaml.
"""
_POLICY = """matching_policy: >
  Prioritise direct professional experience, distinguish production experience
  from independent projects and familiarity, penalise unmet mandatory
  requirements, and select the most relevant dedicated CV for each opportunity.
  Worldwide search does not imply worldwide work authorisation: evaluate
  separately whether the role can be done from your home country (see work_eligibility).
"""


def _block(key: str, value) -> str:
    return yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False, width=92, default_flow_style=False)


def render_profile(p: dict) -> str:
    parts = [_HEADER, "\n"]
    for k in ("name", "headline", "seniority", "experience_years", "home_base"):
        parts.append(_block(k, p[k]))
    parts += ["\n", _ELIGIBILITY_NOTE, _block("work_eligibility", p["work_eligibility"]), _block("languages", p["languages"]),
              "\n", _block("positioning", p["positioning"]), "\n", _FAMILIES_NOTE, _block("families", p["families"]),
              "\n", _block("preferences", p["preferences"]), "\n", _block("capabilities", p["capabilities"]), "\n", _POLICY]
    return "".join(parts)


def _atomic(path: Path, text: str, backup: bool = True) -> None:
    if backup and path.exists():
        path.with_name(path.name + ".bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def apply_profile(cfg, profile: dict) -> Path:
    """Write the profile. Returns the file written (profile.yaml, or profile.suggested.yaml next to a real one)."""
    text = render_profile(profile)
    try:
        got = yaml.safe_load(text)
        got.pop("matching_policy", None)   # fixed explanatory text, not part of what was confirmed
        ok = got == profile
    except (yaml.YAMLError, AttributeError):
        ok = False
    if not ok:
        raise ImportProblem("I could not write the profile safely (it did not read back the same). Nothing was changed.")
    target = cfg.root / "config" / ("profile.yaml" if is_starter_profile(cfg.profile) else "profile.suggested.yaml")
    _atomic(target, text)
    return target


# --- 4. the CV list -----------------------------------------------------------------------

def apply_cvs(cfg, families: list[str], cv_pdf: Path | None = None) -> dict:
    """Give the starter CV list the new families. Returns {'updated': bool, 'unmapped': [...], 'copied': bool}.
    A CV list you edited is never changed: families it does not cover are reported instead."""
    path = cfg.root / "cvs" / "cvs.yaml"
    cvs = cfg.cvs
    if len(cvs) == 1 and cvs[0].get("id") == "general":
        header = path.read_text(encoding="utf-8").split("\ncvs:")[0] + "\n" if path.exists() else ""
        entry = {"id": "general", "file": cvs[0].get("file") or "my-cv.pdf", "families": list(families)}
        text = header + yaml.safe_dump({"cvs": [entry]}, allow_unicode=True, sort_keys=False, width=92)
        if yaml.safe_load(text) != {"cvs": [entry]}:
            raise ImportProblem("I could not write the CV list safely. Nothing was changed.")
        _atomic(path, text)
        copied = False
        dest = cfg.root / "cvs" / entry["file"]
        if cv_pdf and cv_pdf.exists() and not dest.exists():
            dest.write_bytes(cv_pdf.read_bytes())
            copied = True
        return {"updated": True, "unmapped": [], "copied": copied}
    mapped = {f for c in cvs for f in c.get("families", [])}
    return {"updated": False, "unmapped": [f for f in families if f not in mapped], "copied": False}


# --- 5. title keywords (criteria.yaml) ----------------------------------------------------

_LISTS = ("include_title_keywords", "exclude_title_keywords")


def _prefilter_lists(data: dict) -> dict[str, list[str]]:
    pf = (data or {}).get("prefilter") or {}
    return {k: [str(x) for x in pf.get(k) or []] for k in _LISTS}


def criteria_is_starter(cfg) -> bool:
    """True while criteria.yaml is missing or its two title lists are still the packaged starter lists
    (written for a backend-engineering search), so replacing them loses nothing of yours."""
    from .config import DEFAULTS
    path = cfg.root / "config" / "criteria.yaml"
    if not path.exists():
        return True
    mine = _prefilter_lists(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    base = _prefilter_lists(yaml.safe_load((DEFAULTS / "config" / "criteria.yaml").read_text(encoding="utf-8")) or {})
    return all([x.lower() for x in mine[k]] == [x.lower() for x in base[k]] for k in _LISTS)


def conflicts(include: list[str], exclude: list[str]) -> list[str]:
    """Words in the skip list that occur, as whole words, inside a keyword you want: a posting with that
    keyword in its title would be skipped anyway ('recruiter' in both lists rejects every recruiter job)."""
    return [e for e in exclude
            if any(re.search(r"\b" + re.escape(e.lower()) + r"\b", k.lower()) for k in include)]


def starter_excludes(cfg) -> list[str]:
    from .config import DEFAULTS
    path = cfg.root / "config" / "criteria.yaml"
    src = path if path.exists() else DEFAULTS / "config" / "criteria.yaml"
    return _prefilter_lists(yaml.safe_load(src.read_text(encoding="utf-8")) or {})["exclude_title_keywords"]


def _list_span(lines: list[str], key: str) -> tuple[int, int, int, str]:
    """(header line, last item line, header indent, trailing comment) of a YAML list under `key`."""
    pat = re.compile(r"^(\s+)" + key + r":\s*(?:\[\s*\])?\s*(#.*)?$")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            indent, last = len(m.group(1)), i
            for j in range(i + 1, len(lines)):
                if re.match(r"^ {%d,}- " % (indent + 1), lines[j]):
                    last = j
                elif lines[j].strip() and not lines[j].lstrip().startswith("#"):
                    break
            return i, last, indent, m.group(2) or ""
    raise ImportProblem(f"criteria.yaml has no {key} list I can change. Nothing was changed.")


def _item(k: str, indent: int) -> str:
    return " " * indent + yaml.safe_dump([k], allow_unicode=True, default_flow_style=False, width=10**6).strip()


def _replace_items(lines: list[str], key: str, items: list[str]) -> None:
    head, last, indent, comment = _list_span(lines, key)
    tail = ("  " + comment) if comment else ""
    new = [f"{' ' * indent}{key}:{tail}"] + [_item(k, indent + 2) for k in items] if items else [f"{' ' * indent}{key}: []{tail}"]
    lines[head:last + 1] = new


def _append_items(lines: list[str], key: str, items: list[str]) -> None:
    head, last, indent, _ = _list_span(lines, key)
    item_indent = len(lines[last]) - len(lines[last].lstrip(" ")) if last > head else indent + 2
    lines[last + 1:last + 1] = [_item(k, item_indent) for k in items]


def apply_criteria(cfg, include, exclude=None) -> dict:
    """Update the title keywords in criteria.yaml.
    Still the starter lists: both are replaced by `include` / `exclude` (skip words that clash with a wanted
    keyword are dropped and reported). Your own lists: new include keywords are appended, nothing is removed.
    Returns {'mode', 'added', 'removed': [...], 'conflicts': [...]}."""
    from .config import DEFAULTS
    path = cfg.root / "config" / "criteria.yaml"
    text = (path if path.exists() else DEFAULTS / "config" / "criteria.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    have = _prefilter_lists(data)
    starter = criteria_is_starter(cfg)
    want = clean_keywords(include)
    removed: list[str] = []
    if starter:
        inc = want
        exc_in = clean_keywords(exclude) if exclude is not None else [x.lower() for x in have["exclude_title_keywords"]]
        removed = conflicts(inc, exc_in)
        exc = [e for e in exc_in if e not in removed]
        added = len(inc)
    else:
        known = [x.lower() for x in have["include_title_keywords"]]
        new = [k for k in want if k not in known]
        inc, exc, added = have["include_title_keywords"] + new, have["exclude_title_keywords"], len(new)
    left = conflicts(inc, exc)
    out = {"mode": "replaced" if starter else "appended", "added": added, "removed": removed, "conflicts": left}
    if inc == have["include_title_keywords"] and exc == have["exclude_title_keywords"]:
        return {**out, "added": 0}
    lines = text.rstrip("\n").split("\n")
    if starter:
        _replace_items(lines, "include_title_keywords", inc)
        _replace_items(lines, "exclude_title_keywords", exc)
    elif added:
        _append_items(lines, "include_title_keywords", inc[len(have["include_title_keywords"]):])
    expected = copy.deepcopy(data)
    expected.setdefault("prefilter", {})["include_title_keywords"] = inc
    expected["prefilter"]["exclude_title_keywords"] = exc
    new_text = "\n".join(lines) + "\n"
    if yaml.safe_load(new_text) != expected:
        raise ImportProblem("I could not update the keywords in criteria.yaml safely. Nothing was changed.")
    _atomic(path, new_text)
    return out


# --- 6. the pending import (survives a restart) -------------------------------------------

def state_path(cfg) -> Path:
    return cfg.path("db").parent / "profile_import.json"


def cv_copy_path(cfg) -> Path:
    return cfg.path("db").parent / "profile_import_cv.pdf"


def load_state(cfg) -> dict | None:
    try:
        p = state_path(cfg)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def save_state(cfg, state: dict) -> None:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def discard_state(cfg) -> None:
    for p in (state_path(cfg), cv_copy_path(cfg)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# --- 7. background job --------------------------------------------------------------------

Job = fi.Job
CURRENT: Job | None = None
_LOCK = threading.Lock()


def is_running() -> bool:
    return CURRENT is not None and CURRENT.state == "running"


def start(home, make_llm, filename: str, text: str, extra_name: str = "") -> Job:
    global CURRENT
    with _LOCK:
        if is_running():
            raise ImportProblem("A CV is already being read. Wait for it to finish.")
        CURRENT = Job(filename)
    threading.Thread(target=_work, args=(CURRENT, home, make_llm, text, extra_name), daemon=True).start()
    return CURRENT


def _work(job: Job, home, make_llm, text: str, extra_name: str) -> None:
    from .config import load_config
    from .llm import for_stage
    try:
        cfg = load_config(home)
        client = for_stage(make_llm(cfg), "extract")

        def prog(i, n):
            job.message = f"The model is reading (part {i + 1} of {n})…"
        proposals, dropped = extract(client, text, prog)
        fams = [p["value"] for p in proposals if p["kind"] == "family" and p.get("tier")]
        save_state(cfg, {"filename": job.filename, "extra": extra_name, "created": time.time(), "dropped": dropped,
                         "proposals": proposals, "keywords": suggest_keywords(fams)})
        job.state, job.message = "done", f"Found {len(proposals)} suggestions."
    except Exception as e:
        job.state, job.message = "failed", f"{type(e).__name__}: {e}"
    finally:
        job.finished = time.time()
