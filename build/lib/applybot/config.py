from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_ROOT = Path(__file__).resolve().parents[2]   # the repo, when running from a source checkout
DEFAULTS = Path(__file__).resolve().parent / "defaults"   # templates shipped inside the package


def default_home() -> Path:
    """Where your personal files live (config/, cvs/, data/, outbox/).

    1. $APPLYBOT_HOME, if set.
    2. The source checkout, if it is already set up there (config/facts.yaml exists): how earlier
       versions worked, so existing installs keep running unchanged.
    3. ~/.applybot (on Windows, %USERPROFILE%\\.applybot), the same for every installation method."""
    env = os.environ.get("APPLYBOT_HOME")
    if env:
        return Path(env).expanduser()
    if (DEFAULT_ROOT / "config" / "facts.yaml").exists():
        return DEFAULT_ROOT
    return Path.home() / ".applybot"


def _load(path: Path, fallback: Path | None = None) -> dict:
    """YAML file as a dict. A missing file falls back to the packaged default (if given), else {}."""
    if not path.exists():
        if fallback is None or not fallback.exists():
            return {}
        path = fallback
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class Config:
    root: Path
    settings: dict
    profile: dict
    facts: dict
    criteria: dict
    sources: dict
    cvs: list[dict]

    def path(self, key: str) -> Path:
        return self.root / self.settings["paths"][key]

    @property
    def family_names(self) -> list[str]:
        fam = self.profile["families"]
        return list(fam["primary"]) + list(fam["secondary"]) + list(fam["selective"].keys())

    @property
    def family_to_cv(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for cv in self.cvs:
            for fam in cv.get("families", []):
                out[fam] = cv["id"]
        return out

    def cv_file(self, cv_id: str) -> Path | None:
        for cv in self.cvs:
            if cv["id"] == cv_id:
                return self.path("cvs") / cv["file"]
        return None


def load_config(root: Path | str | None = None) -> Config:
    root = Path(root).expanduser() if root else default_home()
    cfg = root / "config"
    cvs = _load(root / "cvs" / "cvs.yaml").get("cvs", [])
    return Config(
        root=root,
        settings=_load(cfg / "settings.yaml"),
        profile=_load(cfg / "profile.yaml"),
        facts=_load(cfg / "facts.yaml"),
        criteria=_load(cfg / "criteria.yaml", DEFAULTS / "config" / "criteria.yaml"),
        sources=_load(cfg / "sources.yaml", DEFAULTS / "config" / "sources.yaml"),
        cvs=cvs,
    )
