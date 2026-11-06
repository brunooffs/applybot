from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
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
    root = Path(root or os.environ.get("APPLYBOT_HOME") or DEFAULT_ROOT)
    cfg = root / "config"
    cvs = _load(root / "cvs" / "cvs.yaml").get("cvs", [])
    return Config(
        root=root,
        settings=_load(cfg / "settings.yaml"),
        profile=_load(cfg / "profile.yaml"),
        facts=_load(cfg / "facts.yaml"),
        criteria=_load(cfg / "criteria.yaml"),
        sources=_load(cfg / "sources.yaml"),
        cvs=cvs,
    )
