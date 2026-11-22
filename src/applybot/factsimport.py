"""Facts from a CV: read the document, let a model PROPOSE facts, verify each against the text,
and write only what the person confirmed.

The model extracts, plain code checks, you decide:
  1. read_document()  text from .txt / .docx (standard library) / .pdf (pypdf). Never leaves the machine.
  2. extract()        the model returns proposals, each with the CV line it came from.
  3. a proposal is DROPPED unless that quote really occurs in the CV text and the thing it names
     (employer, skill, certificate, school...) occurs in the CV too, so the model cannot invent a
     fact and attach a made-up source.
  4. merge_text()     writes the confirmed items into facts.yaml by inserting lines, so comments and
     everything you wrote by hand survive. merge_data() does the same on parsed data; the two
     results must be identical or nothing is written.

A skill level (production / project / working / familiarity) is never inferred: a CV cannot tell
professional work from a hobby. The person picks it, and a skill without a level is not written.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

MAX_DOC_BYTES = 10_000_000
MAX_TEXT_CHARS = 120_000
LEVELS = ("production", "project", "working", "familiarity")
SKILL_CATEGORIES = ("backend", "frontend", "mobile", "api", "distributed", "aws", "cloud", "containers", "iac",
                    "ci_cd", "databases", "messaging", "observability", "testing", "ai", "security", "os", "other")
IDENTITY_FIELDS = ("name", "location", "email", "phone", "linkedin", "github")

# kind -> (fields, field that must literally appear in the CV)
KINDS = {
    "identity": (("field", "value"), "value"),
    "language": (("language", "level"), "language"),
    "experience_total": (("value",), "value"),
    "role": (("employer", "title", "dates", "stack", "work", "highlights"), "employer"),
    "skill": (("category", "item"), "item"),
    "certification": (("name", "issuer", "year"), "name"),
    "education": (("institution", "field", "degree"), "institution"),
}
LIST_FIELDS = ("stack", "highlights")


class ImportProblem(Exception):
    """Something the person can act on (unreadable file, unsafe write). The message is shown as is."""


# --- 1. reading ---------------------------------------------------------------------------

def read_document(filename: str, data: bytes) -> str:
    if len(data) > MAX_DOC_BYTES:
        raise ImportProblem("That file is larger than 10 MB. A CV should be far smaller.")
    ext = Path(filename or "").suffix.lower()
    if ext in (".txt", ".md", ".text", ""):
        text = data.decode("utf-8", "replace")
    elif ext == ".docx":
        text = _docx_text(data)
    elif ext == ".pdf":
        text = _pdf_text(data)
    else:
        raise ImportProblem(f"Unsupported file type '{ext}'. Use PDF, DOCX or plain text.")
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 80:
        raise ImportProblem("I could not read any text from that file. If it is a scanned image, export a text PDF or "
                           "paste the text into a .txt file.")
    return text[:MAX_TEXT_CHARS]


def _docx_text(data: bytes) -> str:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ImportProblem("That does not look like a valid .docx file.") from None
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(xml)   # a .docx is a local file the user chose; no external entities in ElementTree
    out = []
    for p in root.iter(w + "p"):
        parts = []
        for el in p.iter():
            if el.tag == w + "t" and el.text:
                parts.append(el.text)
            elif el.tag in (w + "tab", w + "br"):
                parts.append(" ")
        out.append("".join(parts))
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    import io
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportProblem("PDF reading needs the 'pypdf' package: pip install pypdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ImportProblem("That PDF is password protected.")
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportProblem:
        raise
    except Exception as e:
        raise ImportProblem(f"Could not read that PDF ({type(e).__name__}).") from None


# --- 2. extracting ------------------------------------------------------------------------

SYSTEM = """You extract facts from a CV so a person can review them. Output JSON only.

Return {"items": [ ... ]}. Each item has "kind", "quote" and the fields for its kind:
- identity: field (one of name, location, email, phone, linkedin, github), value
- language: language, level (as written, e.g. "Native", "C1", "fluent")
- experience_total: value (total professional experience exactly as the CV states it, e.g. "20+ years"); only if the CV states it
- role: employer, title, dates (as written), stack (list of technologies named for that job), work (one or two plain sentences of what was done), highlights (list of concrete achievements as written)
- skill: category (one of: backend, frontend, mobile, api, distributed, aws, cloud, containers, iac, ci_cd, databases, messaging, observability, testing, ai, security, os, other), item (one technology or practice named in the CV)
- certification: name, issuer, year
- education: institution, field, degree (as written; do not decide whether it was completed)

Rules:
- "quote" is a short exact span (under 200 characters) copied character for character from the CV, from the line the item comes from. Never invent or paraphrase it.
- Use only what the CV says. Never infer a skill from a job title, never add years, numbers, employers, technologies or achievements the CV does not contain.
- Do not rate skills or say how well someone knows something.
- One item per role, per skill, per certification. Skip anything you are unsure about.
- If a field is not in the CV, leave it out or use "".
"""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"(?<=\w)-\s+(?=\w)", "-", s)   # a word broken across two lines in a PDF
    return re.sub(r"\s+", " ", s).strip()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def appears(value: str, text_norm: str, text_digits: str = "") -> bool:
    v = _norm(value)
    if not v:
        return False
    if v in text_norm:
        return True
    d = _digits(value)
    return len(d) >= 6 and bool(text_digits) and d in text_digits   # phone numbers: spacing differs


def _clean_str(v) -> str:
    return re.sub(r"\s+", " ", str(v)).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""


def _clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,;\n]", v)
    return [x for x in (_clean_str(i) for i in v) if x] if isinstance(v, list) else []


def sanitize(raw) -> list[dict]:
    """Model output of any shape -> a list of well-formed proposals {kind, fields, quote}. Never raises."""
    items = raw.get("items") if isinstance(raw, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or it.get("kind") not in KINDS:
            continue
        kind = it["kind"]
        fields = {}
        for f in KINDS[kind][0]:
            fields[f] = _clean_list(it.get(f)) if f in LIST_FIELDS else _clean_str(it.get(f))
        if kind == "identity" and fields["field"] not in IDENTITY_FIELDS:
            continue
        if kind == "skill":
            fields["category"] = fields["category"] if fields["category"] in SKILL_CATEGORIES else "other"
        if not fields[KINDS[kind][1]] or (kind == "role" and not fields["title"]):
            continue
        out.append({"kind": kind, "fields": fields, "quote": _clean_str(it.get("quote"))})
    return out


def verify(proposals: list[dict], text: str) -> tuple[list[dict], int]:
    """Keep only proposals whose quote occurs in the CV and whose key value occurs in the CV."""
    tn, td = _norm(text), _digits(text)
    kept, dropped = [], 0
    for p in proposals:
        q = _norm(p["quote"])
        anchor = p["fields"][KINDS[p["kind"]][1]]
        if len(q) < 4 or q not in tn or not appears(anchor, tn, td):
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def _chunks(text: str, size: int = 9000) -> list[str]:
    if len(text) <= size + 3000:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > size and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    return out + ([cur] if cur.strip() else [])


def _key(p: dict) -> str:
    f, k = p["fields"], p["kind"]
    main = {"role": lambda: f"{f['employer']}|{f['title']}|{f['dates']}", "skill": lambda: f["item"],
            "identity": lambda: f["field"], "language": lambda: f["language"], "certification": lambda: f["name"],
            "education": lambda: f"{f['institution']}|{f['field']}", "experience_total": lambda: ""}[k]()
    return k + "|" + _norm(main)


def extract(client, text: str, progress=None) -> tuple[list[dict], int]:
    """(proposals, how many the model suggested that failed the checks). `client` has complete_json."""
    seen: dict[str, dict] = {}
    dropped = 0
    parts = _chunks(text)
    for i, chunk in enumerate(parts):
        if progress:
            progress(i, len(parts))
        # A whole CV as JSON is long; the default reply limit would cut it off.
        extra = {"max_tokens": 8000} if hasattr(client, "stage") else {}
        raw = client.complete_json(SYSTEM, "CV TEXT:\n" + chunk, **extra)
        kept, d = verify(sanitize(raw), text)
        dropped += d
        for p in kept:
            seen.setdefault(_key(p), p)
    out = list(seen.values())
    for n, p in enumerate(out, 1):
        p["id"] = f"p{n}"
    return out, dropped


# --- what is already in facts -------------------------------------------------------------

def is_template(facts: dict) -> bool:
    """True while facts.yaml is still the fictional example that `applybot init` copies."""
    return ((facts or {}).get("identity") or {}).get("name") == "Alex Example"


def _unset(v) -> bool:
    return not v or "TODO" in str(v)


def _all_skill_items(facts: dict) -> set[str]:
    out = set()
    for v in (facts.get("skills") or {}).values():
        if isinstance(v, dict):
            out |= {_norm(str(i)) for i in v.get("items") or []}
    return out


_COMPANY_SUFFIX = re.compile(r"\b(b\.?v\.?|n\.?v\.?|ltd\.?|lda\.?|gmbh|inc\.?|llc|s\.?a\.?)\s*$")


def _emp_key(name: str) -> str:
    """Employer name as people write it differently: 'Netwerven B.V. / Endouble (acquired ...)' = 'Netwerven'."""
    n = _norm(re.sub(r"\(.*?\)", " ", name or ""))
    n = n.split(" / ")[0].strip(" ,.-")
    return _COMPANY_SUFFIX.sub("", n).strip(" ,.-")


def _years(dates: str) -> tuple:
    """('2022', '2025') from '2022 to 2025', '2022–2025' or 'May 2025 to present'; 'present' -> 'now'."""
    d = _norm(dates)
    ys = re.findall(r"\b(?:19|20)\d\d\b", d)
    if not ys:
        return ()
    end = "now" if re.search(r"present|current|now", d) else ys[-1]
    return (ys[0], end)


def _same_role(r: dict, f: dict) -> bool:
    """Same job: same years when both have them (titles differ between CV versions), else same title."""
    a, b = _years(str(r.get("dates", ""))), _years(f.get("dates", ""))
    if a and b:
        return a == b
    return _norm(str(r.get("title", ""))) == _norm(f["title"]) and _norm(str(r.get("dates", ""))) == _norm(f.get("dates", ""))


def _find_employer(facts: dict, name: str):
    key = _emp_key(name)
    for e in ((facts.get("experience") or {}).get("employers") or []):
        if isinstance(e, dict) and key and _emp_key(str(e.get("name", ""))) == key:
            return e
    return None


def mark_existing(facts: dict, proposals: list[dict]) -> None:
    """Set p['existing'] for proposals whose content is already in facts (they are shown apart)."""
    skills = _all_skill_items(facts)
    for p in proposals:
        f, k = p["fields"], p["kind"]
        ex = False
        if k == "identity":
            ex = not _unset((facts.get("identity") or {}).get(f["field"]))
        elif k == "language":
            ex = _norm(f["language"]) in {_norm(x) for x in (facts.get("language_levels") or {})}
        elif k == "experience_total":
            ex = not _unset((facts.get("experience") or {}).get("total"))
        elif k == "role":
            e = _find_employer(facts, f["employer"])
            ex = bool(e) and any(_same_role(r, f) for r in e.get("roles") or [] if isinstance(r, dict))
        elif k == "skill":
            ex = _norm(f["item"]) in skills
        elif k == "certification":
            ex = _norm(f["name"]) in {_norm(str(c.get("name", ""))) for c in facts.get("certifications") or [] if isinstance(c, dict)}
        elif k == "education":
            ex = any(_norm(str(e.get("institution", ""))) == _norm(f["institution"]) and
                     _norm(str(e.get("field", ""))) == _norm(f["field"])
                     for e in facts.get("education") or [] if isinstance(e, dict))
        p["existing"] = ex


# --- 4a. merging into parsed data (the reference semantics) -------------------------------

def _role_dict(f: dict) -> dict:
    r = {"title": f["title"]}
    for k in ("dates", "stack", "work", "highlights"):
        if f.get(k):
            r[k] = f[k]
    return r


def _cat_key(cat: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", cat.lower().replace(" ", "_")) or "other"


def merge_data(old: dict, items: list[dict]) -> dict:
    d = copy.deepcopy(old or {})
    for it in items:
        k, f = it["kind"], it["fields"]
        if k == "identity":
            ident = d.setdefault("identity", {})
            if _unset(ident.get(f["field"])):
                ident[f["field"]] = f["value"]
        elif k == "language":
            d.setdefault("language_levels", {}).setdefault(f["language"], f["level"] or "TODO")
        elif k == "experience_total":
            ex = d.setdefault("experience", {})
            if _unset(ex.get("total")):
                ex["total"] = f["value"]
        elif k == "role":
            emps = d.setdefault("experience", {}).setdefault("employers", [])
            e = _find_employer(d, f["employer"])
            role = _role_dict(f)
            if e is None:
                emps.append({"name": f["employer"], "roles": [role]})
            else:
                roles = e.setdefault("roles", [])
                if not any(_same_role(r, f) for r in roles if isinstance(r, dict)):
                    roles.append(role)
        elif k == "skill":
            if not f.get("level") or f["level"] not in LEVELS or _norm(f["item"]) in _all_skill_items(d):
                continue
            sk = d.setdefault("skills", {})
            cat = _cat_key(f["category"])
            if cat in sk and isinstance(sk[cat], dict) and sk[cat].get("level") != f["level"]:
                cat = f"{cat}_{f['level']}"
            entry = sk.setdefault(cat, {"level": f["level"], "items": []})
            entry.setdefault("items", []).append(f["item"])
        elif k == "certification":
            certs = d.setdefault("certifications", [])
            if _norm(f["name"]) not in {_norm(str(c.get("name", ""))) for c in certs if isinstance(c, dict)}:
                c = {"name": f["name"]}
                for kk in ("issuer", "year"):
                    if f.get(kk):
                        c[kk] = int(f[kk]) if kk == "year" and f[kk].isdigit() else f[kk]
                certs.append(c)
        elif k == "education":
            edu = d.setdefault("education", [])
            if not any(_norm(str(e.get("institution", ""))) == _norm(f["institution"]) and
                       _norm(str(e.get("field", ""))) == _norm(f["field"]) for e in edu if isinstance(e, dict)):
                e = {"institution": f["institution"]}
                if f.get("field"):
                    e["field"] = f["field"]
                e["degree"] = f.get("degree") or "TODO"   # letters must not say "degree" until the person sets it
                edu.append(e)
    return d


# --- 4b. merging into the text, keeping comments ------------------------------------------

def _ind(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


class _Dumper(yaml.SafeDumper):
    """Matches the file's style: block mappings, indented lists, short lists of plain values in [a, b] form."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _rep_list(dumper, data):
    flow = all(isinstance(x, (str, int, float, bool)) or x is None for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


def _rep_dict(dumper, data):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=False)


_Dumper.add_representer(list, _rep_list)
_Dumper.add_representer(dict, _rep_dict)


def _dump(obj, indent: int = 0) -> list[str]:
    text = yaml.dump(obj, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10**6)
    return [" " * indent + ln if ln else ln for ln in text.rstrip("\n").split("\n")]


def _find_key(lines: list[str], key: str, lo: int, hi: int, indent: int | None = None) -> int | None:
    pat = re.compile(r"^( *)" + re.escape(key) + r":(\s|$)")
    for i in range(lo, hi):
        m = pat.match(lines[i])
        if m and (indent is None or len(m.group(1)) == indent):
            return i
    return None


def _block_end(lines: list[str], idx: int, hi: int | None = None) -> int:
    """Index just after the last content line of the block that starts at lines[idx]."""
    hi = len(lines) if hi is None else hi
    base, last = _ind(lines[idx]), idx
    for j in range(idx + 1, hi):
        ln = lines[j]
        if not _is_content(ln):
            continue
        if _ind(ln) > base or (_ind(ln) == base and ln.lstrip().startswith("- ")):
            last = j
        else:
            break
    return last + 1


def _open_block(lines: list[str], idx: int) -> None:
    """`key: []` or `key: {}` -> `key:` so entries can be added under it."""
    m = re.match(r"^( *[\w-]+):\s*(\[\s*\]|\{\s*\})\s*(#.*)?$", lines[idx])
    if m:
        lines[idx] = m.group(1) + ":" + (("  " + m.group(3)) if m.group(3) else "")


def _child_indent(lines: list[str], idx: int, end: int) -> int:
    """Indent used by the entries under lines[idx] (existing ones, else two deeper)."""
    for j in range(idx + 1, end):
        if _is_content(lines[j]):
            return _ind(lines[j])
    return _ind(lines[idx]) + 2


def _section(lines: list[str], key: str, parent: int | None = None):
    """(idx, end) of a top-level key, or of a key nested under the block `parent` = (idx, end)."""
    if parent is None:
        idx = _find_key(lines, key, 0, len(lines), 0)
    else:
        idx = _find_key(lines, key, parent[0] + 1, parent[1], _child_indent(lines, parent[0], parent[1]))
    if idx is None:
        return None
    _open_block(lines, idx)
    return idx, _block_end(lines, idx)


def _scalar_line(key: str, value, indent: int) -> str:
    v = re.sub(r"\s+", " ", str(value)) if isinstance(value, str) else value
    return " " * indent + yaml.safe_dump({key: v}, allow_unicode=True, width=10**6).strip()


def _flow(lst) -> str:
    return yaml.safe_dump(list(lst), default_flow_style=True, allow_unicode=True, width=10**6).strip()


def _set_scalar(lines: list[str], block, key: str, value) -> None:
    idx, end = block
    kidx = _find_key(lines, key, idx + 1, end, _child_indent(lines, idx, end))
    if kidx is not None:
        old = lines[kidx]
        trail = re.search(r"\s+(#.*)$", old)
        lines[kidx] = _scalar_line(key, value, _ind(old)) + (("  " + trail.group(1)) if trail else "")
    else:
        lines.insert(end, _scalar_line(key, value, _child_indent(lines, idx, end)))


def _append_top(lines: list[str], key: str, body: list[str]) -> None:
    while lines and not lines[-1].strip():
        lines.pop()
    lines.extend(["", f"{key}:"] + body)


def _append_list_item(lines: list[str], key: str, item: dict) -> None:
    sec = _section(lines, key)
    if sec is None:
        _append_top(lines, key, _dump([item], 2))
        return
    idx, end = sec
    lines[end:end] = _dump([item], _child_indent(lines, idx, end))


def merge_text(text: str, items: list[dict]) -> str:
    lines = text.rstrip("\n").split("\n")
    working = merge_data(yaml.safe_load(text) or {}, [])   # parsed copy kept in step to decide what is new
    for it in items:
        k, f = it["kind"], it["fields"]
        before = copy.deepcopy(working)
        working = merge_data(working, [it])
        if working == before:
            continue   # already present / not writable: same rule as merge_data
        if k == "identity":
            sec = _section(lines, "identity")
            if sec is None:
                _append_top(lines, "identity", [_scalar_line(f["field"], f["value"], 2)])
            else:
                _set_scalar(lines, sec, f["field"], f["value"])
        elif k == "language":
            sec = _section(lines, "language_levels")
            line = lambda ind: _scalar_line(f["language"], f["level"] or "TODO", ind)   # noqa: E731
            if sec is None:
                _append_top(lines, "language_levels", [line(2)])
            else:
                lines.insert(sec[1], line(_child_indent(lines, *sec)))
        elif k == "experience_total":
            sec = _section(lines, "experience")
            if sec is None:
                _append_top(lines, "experience", [_scalar_line("total", f["value"], 2)])
            else:
                _set_scalar(lines, sec, "total", f["value"])
        elif k == "role":
            _write_role(lines, f)
        elif k == "skill":
            _write_skill(lines, before, f)
        elif k == "certification":
            c = {"name": f["name"], **{kk: (int(f[kk]) if kk == "year" and f[kk].isdigit() else f[kk])
                                       for kk in ("issuer", "year") if f.get(kk)}}
            _append_list_item(lines, "certifications", c)
        elif k == "education":
            e = {"institution": f["institution"], **({"field": f["field"]} if f.get("field") else {}),
                 "degree": f.get("degree") or "TODO"}
            _append_list_item(lines, "education", e)
    return "\n".join(lines) + "\n"


def _write_role(lines: list[str], f: dict) -> None:
    role = _role_dict(f)
    exp = _section(lines, "experience")
    if exp is None:
        _append_top(lines, "experience", ["  employers:"] + _dump([{"name": f["employer"], "roles": [role]}], 4))
        return
    emps = _section(lines, "employers", exp)
    if emps is None:
        lines[exp[1]:exp[1]] = ["  employers:"] + _dump([{"name": f["employer"], "roles": [role]}], 4)
        return
    e_idx, e_end = emps
    ci = _child_indent(lines, e_idx, e_end)
    pat = re.compile(r"^ {%d}- name:\s*(.+?)\s*(#.*)?$" % ci)
    starts = [i for i in range(e_idx + 1, e_end) if re.match(r"^ {%d}- " % ci, lines[i])]
    for n, s in enumerate(starts):
        m = pat.match(lines[s])
        if m and _emp_key(m.group(1).strip("'\"")) == _emp_key(f["employer"]):
            stop = starts[n + 1] if n + 1 < len(starts) else e_end
            r_idx = _find_key(lines, "roles", s, stop, ci + 2)
            if r_idx is None:
                lines[stop:stop] = [" " * (ci + 2) + "roles:"] + _dump([role], ci + 4)
            else:
                _open_block(lines, r_idx)
                r_end = _block_end(lines, r_idx, stop)
                lines[r_end:r_end] = _dump([role], _child_indent(lines, r_idx, r_end))
            return
    lines[e_end:e_end] = _dump([{"name": f["employer"], "roles": [role]}], ci)


def _write_skill(lines: list[str], before: dict, f: dict) -> None:
    sk = _section(lines, "skills")
    cat = _cat_key(f["category"])
    existing = (before.get("skills") or {}).get(cat)
    if isinstance(existing, dict) and existing.get("level") != f["level"]:
        cat = f"{cat}_{f['level']}"
        existing = (before.get("skills") or {}).get(cat)
    if sk is None:
        _append_top(lines, "skills", [f"  {cat}: {{level: {f['level']}, items: {_flow([f['item']])}}}"])
        return
    idx, end = sk
    if isinstance(existing, dict):
        k_idx = _find_key(lines, cat, idx + 1, end, _child_indent(lines, idx, end))
        m = re.match(r"^( *)" + re.escape(cat) + r":\s*\{(.*)\}\s*(#.*)?$", lines[k_idx] if k_idx is not None else "")
        if not m:
            raise ImportProblem(f"The skills.{cat} entry in facts.yaml is not on one line, so I cannot add to it safely.")
        line = lines[k_idx]
        im = re.search(r"items:\s*\[(.*?)\]", line)
        if not im:
            raise ImportProblem(f"The skills.{cat} entry in facts.yaml has no one-line items list, so I cannot add to it safely.")
        add = _flow([f["item"]])[1:-1]
        lines[k_idx] = line[:im.end() - 1] + (", " if im.group(1).strip() else "") + add + line[im.end() - 1:]
        return
    lines.insert(end, " " * _child_indent(lines, idx, end) + f"{cat}: {{level: {f['level']}, items: {_flow([f['item']])}}}")


SKELETON = """# VERIFIED FACTS: the ONLY source the letter generator and the fact-checker may use.
# Anything not here cannot appear in a letter. Created from your CV by the Facts page;
# lines marked TODO need your answer (letters will not state them until you do).
#
# Skill levels: production (professional systems), project (independent work only),
# working (can work with it), familiarity (concepts only), none (do not claim).

identity:
  name: TODO
  location: TODO

experience:
  total: TODO
  employers: []

certifications: []

education: []

skills: {}

not_claimed: []

language_levels: {}

availability: TODO
"""


def apply_import(cfg, items: list[dict], from_skeleton: bool) -> dict:
    """Write confirmed items into config/facts.yaml. Returns {'written': n}. Raises ImportProblem if unsafe.

    from_skeleton: the file is still the fictional example, so start from a blank skeleton (the old
    file is kept as facts.yaml.bak). The new text must parse and equal merge_data(old, items) first."""
    path = cfg.root / "config" / "facts.yaml"
    old_text = path.read_text(encoding="utf-8") if path.exists() else ""
    base_text = SKELETON if (from_skeleton or not old_text.strip()) else old_text
    base = yaml.safe_load(base_text) or {}
    expected = merge_data(base, items)
    try:
        new_text = merge_text(base_text, items)
        got = yaml.safe_load(new_text)
    except ImportProblem:
        raise
    except Exception as e:
        raise ImportProblem(f"I could not add these to facts.yaml safely ({type(e).__name__}). Nothing was changed.") from None
    if got != expected:
        raise ImportProblem("I could not add these to facts.yaml safely (the result did not match what was confirmed). "
                           "Nothing was changed.")
    if old_text:
        path.with_name("facts.yaml.bak").write_text(old_text, encoding="utf-8")
    tmp = path.with_name("facts.yaml.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return {"written": sum(1 for _ in items)}


# --- the pending import (survives a restart) ----------------------------------------------

def state_path(cfg) -> Path:
    return cfg.path("db").parent / "facts_import.json"


def load_state(cfg) -> dict | None:
    p = state_path(cfg)
    try:
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
    state_path(cfg).unlink(missing_ok=True)


# --- running the extraction in the background ---------------------------------------------

class Job:
    def __init__(self, filename: str):
        self.filename = filename
        self.state = "running"      # running | done | failed
        self.message = "Reading the CV…"
        self.started = time.time()
        self.finished: float | None = None


CURRENT: Job | None = None
_LOCK = threading.Lock()


def is_running() -> bool:
    return CURRENT is not None and CURRENT.state == "running"


def start(home, make_llm, filename: str, text: str) -> Job:
    global CURRENT
    with _LOCK:
        if is_running():
            raise ImportProblem("A CV is already being read. Wait for it to finish.")
        CURRENT = Job(filename)
    threading.Thread(target=_work, args=(CURRENT, home, make_llm, text), daemon=True).start()
    return CURRENT


def _work(job: Job, home, make_llm, text: str) -> None:
    from .config import load_config
    from .llm import for_stage
    try:
        cfg = load_config(home)
        client = for_stage(make_llm(cfg), "extract")

        def prog(i, n):
            job.message = f"The model is reading the CV (part {i + 1} of {n})…"
        proposals, dropped = extract(client, text, prog)
        # while facts.yaml is still the fictional example, compare against nothing: it will be replaced
        mark_existing({} if is_template(cfg.facts) else cfg.facts, proposals)
        save_state(cfg, {"filename": job.filename, "created": time.time(), "dropped": dropped,
                         "replace_example": is_template(cfg.facts), "proposals": proposals})
        job.state, job.message = "done", f"Found {len(proposals)} items."
    except Exception as e:
        job.state, job.message = "failed", f"{type(e).__name__}: {e}"
    finally:
        job.finished = time.time()
