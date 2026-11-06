"""Job source adapters.

Each adapter has a pure `parse_*` function (testable offline) and a `fetch`
generator that does the HTTP. Field names follow each vendor's documented
public board API and are NOT yet verified against live responses.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

import httpx

from ..models import Job
from . import ashby, greenhouse, lever, remotive

log = logging.getLogger("applybot.sources")


def iter_jobs(cfg) -> Iterator[Job]:
    headers = {"User-Agent": cfg.settings["http"]["user_agent"]}
    delay = cfg.settings["http"].get("delay_seconds", 1.0)
    src = cfg.sources
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as http:
        for mod, key in ((greenhouse, "greenhouse"), (lever, "lever"), (ashby, "ashby")):
            for token in src.get(key) or []:
                try:
                    yield from mod.fetch(token, http)
                except Exception as e:  # one bad board must not stop the run
                    log.warning("%s/%s failed: %s", key, token, e)
                time.sleep(delay)
        rem = src.get("remotive") or {}
        if rem.get("enabled"):
            for cat in rem.get("categories") or [None]:
                try:
                    yield from remotive.fetch(cat, http)
                except Exception as e:
                    log.warning("remotive/%s failed: %s", cat, e)
                time.sleep(delay)
