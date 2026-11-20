"""Where can the candidate work? A deterministic check on the posting's location string.

A small local model judges "can this be done from my country" unreliably. The job's own location
field ("Remote US", "Remote Poland", "Berlin, Germany") is usually decisive, so plain code reads it
first and only falls back to the model when the string says nothing it recognises.

Configured in profile.yaml:

    work_eligibility:
      home_country: Portugal
      authorised_regions: [Portugal, EU, EEA]   # places you can work without a visa or permit
      accepts_relocation: false                 # true: an unreachable place goes to review, not reject
"""
from __future__ import annotations

import re

EU = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "denmark", "estonia", "finland",
    "france", "germany", "greece", "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg",
    "malta", "netherlands", "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
}
EEA = EU | {"norway", "iceland", "liechtenstein"}
EUROPE = EEA | {"united kingdom", "switzerland", "serbia", "ukraine", "albania", "bosnia", "moldova",
                "montenegro", "north macedonia", "turkey", "georgia", "armenia", "belarus", "kosovo"}
MIDDLE_EAST_AFRICA = {"israel", "united arab emirates", "saudi arabia", "egypt", "south africa", "nigeria", "kenya",
                      "morocco", "qatar"}
AMERICAS = {"united states", "canada", "mexico", "brazil", "argentina", "colombia", "chile", "peru", "costa rica"}
APAC = {"india", "china", "japan", "singapore", "australia", "new zealand", "philippines", "vietnam",
        "indonesia", "south korea", "malaysia", "thailand", "pakistan", "bangladesh", "taiwan", "hong kong"}
EVERYWHERE = EUROPE | MIDDLE_EAST_AFRICA | AMERICAS | APAC

# place name as it appears in postings -> canonical country, or a region name
ALIASES = {
    "usa": "united states", "u.s.": "united states", "u.s.a.": "united states", "us": "united states",
    "united states of america": "united states", "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "great britain": "united kingdom", "england": "united kingdom",
    "scotland": "united kingdom", "wales": "united kingdom", "northern ireland": "united kingdom",
    "czech republic": "czechia", "the netherlands": "netherlands", "holland": "netherlands",
    "uae": "united arab emirates", "korea": "south korea", "bosnia and herzegovina": "bosnia",
    "españa": "spain", "deutschland": "germany", "polska": "poland", "brasil": "brazil",
}
REGIONS = {
    "eu": EU, "european union": EU, "eea": EEA, "europe": EUROPE, "emea": EUROPE | MIDDLE_EAST_AFRICA,
    "dach": {"germany", "austria", "switzerland"}, "nordics": {"sweden", "norway", "denmark", "finland", "iceland"},
    "north america": {"united states", "canada", "mexico"}, "americas": AMERICAS, "latam": AMERICAS - {"united states", "canada"},
    "latin america": AMERICAS - {"united states", "canada"}, "apac": APAC, "asia": APAC,
    "worldwide": EVERYWHERE, "global": EVERYWHERE, "anywhere": EVERYWHERE, "international": EVERYWHERE,
    "world": EVERYWHERE,
}
COUNTRIES = EUROPE | MIDDLE_EAST_AFRICA | AMERICAS | APAC

# Longest names first so "united states" wins over "us", "north america" over "america".
_NAMES = sorted(set(COUNTRIES) | set(ALIASES) | set(REGIONS), key=len, reverse=True)
_PATTERN = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in _NAMES) + r")(?![\w])", re.I)
# "US" as a word is ambiguous in running text ("join us"), so it only counts in a location field.
_OPEN_TO_ANYONE = re.compile(r"\b(b2b|independent contractor|contractor|employer of record|\beor\b|"
                             r"hire (?:in|from) (?:any|all|over)|worldwide|anywhere in the world)\b", re.I)


def _resolve(name: str) -> set[str]:
    """A place name -> the set of countries it covers."""
    n = ALIASES.get(name.lower(), name.lower())
    if n in REGIONS:
        return set(REGIONS[n])
    return {n} if n in COUNTRIES else set()


def places(location: str) -> list[str]:
    """Recognised places named in a job's location string, in order of appearance."""
    return [m.group(1).lower() for m in _PATTERN.finditer(location or "")]


def authorised_set(we: dict) -> set[str]:
    out: set[str] = set()
    for entry in list(we.get("authorised_regions") or []) + [we.get("home_country")]:
        if entry:
            out |= _resolve(str(entry))
    return out


def location_eligibility(location: str, description: str, we: dict | None) -> tuple[str | None, str]:
    """('yes' | 'no' | None, reason). None = the location string settles nothing: ask the model.

    'no' only when every recognised place is outside the candidate's authorised set, so one open
    place in a list ("Remote Spain; Remote US") keeps the job in play. Postings that say they hire
    contractors, B2B or through an employer of record are never auto-rejected on location."""
    we = we or {}
    ok = authorised_set(we)
    if not ok:
        return None, "no work_eligibility configured"
    found = places(location)
    if not found:
        return None, "location names no recognised place"
    covers = [_resolve(p) for p in found]
    if any(c & ok for c in covers):
        return "yes", f"location '{location}' includes a place you can work from"
    if _OPEN_TO_ANYONE.search(description or ""):
        return None, "posting mentions contractors or employer of record"
    if we.get("accepts_relocation"):
        return None, "outside your authorised regions, but you accept relocation"
    return "no", f"location '{location}' is outside your authorised regions"


def describe(we: dict | None, home_base: str | None = None) -> str:
    """The candidate's eligibility, as lines for the scoring prompt."""
    we = dict(we or {})
    home = we.get("home_country") or home_base or "unknown"
    lines = [f"home_country: {home}"]
    regions = we.get("authorised_regions") or [home]
    lines.append("authorised_regions (can work there without visa or permit): " + ", ".join(map(str, regions)))
    lines.append(f"accepts_relocation: {bool(we.get('accepts_relocation', False))}")
    return "\n".join(lines)


def home_country(profile: dict) -> str:
    we = profile.get("work_eligibility") or {}
    return we.get("home_country") or profile.get("home_base") or "your location"
