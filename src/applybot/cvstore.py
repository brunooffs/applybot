"""Keeping an uploaded CV (and cover letter) in the cvs folder and in cvs/cvs.yaml.

  * The file is saved in cvs/ under its own name. A file with the same name is replaced, without asking.
  * A CV that is not in cvs/cvs.yaml yet gets an entry. While the list is still the one-CV starter
    (my-cv.pdf, which does not exist) the upload takes that entry over instead of adding a second one.
  * A cover letter goes to cvs/cover-letters/. It is kept for later and is not a CV entry.
  * Job families are given to a CV with set_families(); a family that already belongs to another CV stays there.
cvs.yaml is rewritten from the parsed list (the comment header is kept) and must read back identical.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .factsimport import ImportProblem, _Dumper

ALLOWED = (".pdf", ".docx", ".txt", ".md")
LETTERS = "cover-letters"
DEFAULT_HEADER = """# CV pool. Each entry is a CV file in this folder and the job families it is used for.
# A job family is mapped to exactly one CV. Families with NO entry here are sent to human review
# with reason "no CV mapped", they are never guessed. Family names must match config/profile.yaml.

"""


def safe_name(filename: str) -> str:
    """The file's own name, made safe to store: no folders, no odd characters, a known extension."""
    base = Path(str(filename or "").replace("\\", "/")).name
    stem, ext = os.path.splitext(re.sub(r"[^\w.\- ]", "_", base).strip(" ."))
    if ext.lower() not in ALLOWED or not stem.strip(" ._-"):
        raise ImportProblem("Use a PDF, DOCX or text file with a normal name.")
    return stem + ext.lower()


def slug(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "cv"


def _read(root: Path) -> tuple[str, list[dict], dict]:
    path = root / "cvs" / "cvs.yaml"
    if not path.exists():
        return DEFAULT_HEADER, [], {}
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    cvs = [dict(c) for c in data.get("cvs") or [] if isinstance(c, dict)]
    return re.split(r"(?m)^cvs:", text, maxsplit=1)[0], cvs, {k: v for k, v in data.items() if k != "cvs"}


def _write(root: Path, header: str, cvs: list[dict], extra: dict) -> None:
    data = {"cvs": cvs, **extra}
    text = header + yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    if yaml.safe_load(text) != data:
        raise ImportProblem("I could not update cvs.yaml safely. Nothing was changed.")
    path = root / "cvs" / "cvs.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name("cvs.yaml.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_name("cvs.yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _put(path: Path, data: bytes) -> bool:
    """Write the file, replacing one of the same name. True if there was one."""
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return existed


def _unique_id(base: str, cvs: list[dict]) -> str:
    ids, n, out = {c.get("id") for c in cvs}, 1, base
    while out in ids:
        n += 1
        out = f"{base}-{n}"
    return out


def store_cv(root: Path, filename: str, data: bytes) -> dict:
    """Save an uploaded CV in cvs/ and make sure cvs.yaml lists it. Returns {file, id, replaced, added}."""
    name = safe_name(filename)
    folder = root / "cvs"
    header, cvs, extra = _read(root)
    starter = (len(cvs) == 1 and cvs[0].get("id") == "general" and cvs[0].get("file") == "my-cv.pdf"
               and not (folder / "my-cv.pdf").exists() and name != "my-cv.pdf")
    replaced = _put(folder / name, data)
    entry = next((c for c in cvs if c.get("file") == name), None)
    added = entry is None
    if starter and added:
        entry = cvs[0]
        entry["id"], entry["file"] = _unique_id(slug(Path(name).stem), []), name
    elif added:
        entry = {"id": _unique_id(slug(Path(name).stem), cvs), "file": name, "families": []}
        cvs.append(entry)
    if added:
        _write(root, header, cvs, extra)
    return {"file": name, "id": entry["id"], "replaced": replaced, "added": added}


def store_letter(root: Path, filename: str, data: bytes) -> Path:
    """Keep a cover letter for later, in cvs/cover-letters/. Not a CV entry."""
    path = root / "cvs" / LETTERS / safe_name(filename)
    _put(path, data)
    return path


def set_families(root: Path, cv_file: str, families: list[str], exclusive: bool = False) -> dict:
    """Give job families to the CV with this file name. A family that belongs to another CV stays there.
    exclusive: when this is the only CV, its families become exactly `families` (the starter's placeholders go).
    Returns {'cv': id, 'added': [...], 'elsewhere': {family: cv id}}."""
    header, cvs, extra = _read(root)
    entry = next((c for c in cvs if c.get("file") == cv_file), None)
    if entry is None:
        raise ImportProblem(f"{cv_file} is not in cvs/cvs.yaml, so I could not give it your job families.")
    if exclusive and len(cvs) == 1:
        added = [f for f in families if f not in (entry.get("families") or [])]
        entry["families"], elsewhere = list(dict.fromkeys(families)), {}
    else:
        owner = {f: c.get("id") for c in cvs if c is not entry for f in c.get("families") or []}
        have = entry.setdefault("families", [])
        added, elsewhere = [], {}
        for f in dict.fromkeys(families):
            if f in have:
                continue
            if f in owner:
                elsewhere[f] = owner[f]
            else:
                have.append(f)
                added.append(f)
    if added or exclusive:
        _write(root, header, cvs, extra)
    return {"cv": entry["id"], "added": added, "elsewhere": elsewhere}
