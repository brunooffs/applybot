from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


@dataclass
class Job:
    source: str
    ext_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: Optional[bool] = None
    description: str = ""
    employment_type: str = ""
    posted_at: str = ""

    @property
    def id(self) -> str:
        return f"{self.source}:{self.ext_id}"

    @property
    def dedupe_key(self) -> str:
        return "|".join(_norm(x) for x in (self.company, self.title, self.location))
