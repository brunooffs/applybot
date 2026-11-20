"""`applybot doctor`: is this machine ready, and what should be installed next?

Read-only. It never installs, downloads or changes anything; it reports what it finds and prints
the exact command to run when something is missing. Ollama itself is never installed from here.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys

import httpx
import yaml

from .config import DEFAULTS, Config

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def ram_gb() -> float | None:
    """Installed RAM in GB, or None if it cannot be read."""
    try:
        if sys.platform == "darwin":
            return int(_run(["sysctl", "-n", "hw.memsize"])) / 1024**3
        if sys.platform.startswith("linux"):
            for line in open("/proc/meminfo", encoding="utf-8"):
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024**2
        if sys.platform == "win32":
            import ctypes

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))   # type: ignore[attr-defined]
            return st.ullTotalPhys / 1024**3
    except (OSError, ValueError, AttributeError):
        pass
    return None


def nvidia_gpu() -> tuple[str, float] | None:
    """(name, video memory in GB) of the first NVIDIA GPU, if nvidia-smi is installed."""
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    if not out:
        return None
    name, _, mib = out.splitlines()[0].rpartition(",")
    try:
        return name.strip(), float(mib) / 1024
    except ValueError:
        return None


def hardware() -> dict:
    """What matters for running a local model: OS, chip, RAM, GPU, and the memory a model can use."""
    ram = ram_gb()
    machine = platform.machine().lower()
    apple = sys.platform == "darwin" and machine in ("arm64", "aarch64")
    gpu = nvidia_gpu()
    if apple and ram:
        usable, kind = ram, "Apple silicon (memory is shared with the GPU)"
    elif gpu:
        usable, kind = gpu[1], f"NVIDIA {gpu[0]}"
    elif ram:
        usable, kind = ram / 2, "no supported GPU found, so the model would run on the CPU and be slow"
    else:
        usable, kind = None, "unknown"
    system = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform)
    return {"system": system, "machine": machine, "ram_gb": ram, "usable_gb": usable, "kind": kind, "apple_silicon": apple}


def recommend(usable_gb: float | None, tiers: list[dict] | None = None) -> dict | None:
    """The highest tier whose min_gb the machine meets. None if memory is unknown."""
    if usable_gb is None:
        return None
    tiers = tiers if tiers is not None else (yaml.safe_load((DEFAULTS / "models.yaml").read_text(encoding="utf-8")) or {}).get("tiers", [])
    fit = [t for t in tiers if usable_gb >= t.get("min_gb", 0)]
    return max(fit, key=lambda t: t.get("min_gb", 0)) if fit else None


def ollama_models(host: str) -> list[str] | None:
    """Names of the models Ollama has pulled, or None if it is not reachable."""
    try:
        r = httpx.get(host.rstrip("/") + "/api/tags", timeout=3)
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None


def has_model(pulled: list[str], wanted: str) -> bool:
    return any(p == wanted or (":" not in wanted and p == wanted + ":latest") for p in pulled)


def open_items(text: str) -> int:
    """How many TODO / CONFIRM markers a file still holds."""
    return len(re.findall(r"\b(TODO|CONFIRM)\b", text))


def checks(cfg: Config) -> list[tuple[str, str]]:
    from .llm import STAGES
    out: list[tuple[str, str]] = []
    add = lambda level, msg: out.append((level, msg))

    add(OK, f"Python {platform.python_version()}")
    add(INFO, f"applybot folder: {cfg.root}")

    # your files
    for rel, what in (("config/facts.yaml", "your facts"), ("config/profile.yaml", "your profile"),
                      ("config/settings.yaml", "settings"), ("cvs/cvs.yaml", "the CV list")):
        p = cfg.root / rel
        if not p.exists():
            add(FAIL, f"{rel} is missing ({what}). Run `applybot init`.")
        elif rel in ("config/facts.yaml", "config/profile.yaml"):
            n = open_items(p.read_text(encoding="utf-8"))
            add(WARN if n else OK, f"{rel}: {n} TODO/CONFIRM line(s) left to resolve" if n else f"{rel}: no TODO/CONFIRM left")
    if cfg.cvs:
        missing = [c["id"] for c in cfg.cvs if not (cfg.cv_file(c["id"]) and cfg.cv_file(c["id"]).exists())]
        add(WARN if missing else OK, "CV file(s) not found in cvs/: " + ", ".join(missing) if missing else f"{len(cfg.cvs)} CV file(s) found")

    # this computer
    hw = hardware()
    ram = f"{hw['ram_gb']:.0f} GB RAM" if hw["ram_gb"] else "RAM unknown"
    add(INFO, f"This computer: {hw['system']} ({hw['machine']}), {ram}; {hw['kind']}")
    rec = recommend(hw["usable_gb"])
    if rec:
        add(INFO, "Local model suggestion: " + (f"`ollama pull {rec['model']}`. " if rec.get("model") else "") + str(rec.get("note", "")))

    # models in use
    llm = cfg.settings.get("llm", {})
    default, stages = llm.get("provider", "ollama"), llm.get("stages") or {}
    used = {stages.get(s, default) for s in STAGES}
    if "anthropic" in used:
        if os.environ.get("ANTHROPIC_API_KEY"):
            add(OK, "ANTHROPIC_API_KEY is set in this terminal (not tested: a wrong key only shows when a stage runs)")
        else:
            add(FAIL, "A stage uses anthropic but ANTHROPIC_API_KEY is not set in this terminal. "
                      "Set it without typing it into history: macOS/Linux `read -rs ANTHROPIC_API_KEY; export ANTHROPIC_API_KEY`; "
                      "Windows PowerShell `$k = Read-Host 'API key' -AsSecureString; $env:ANTHROPIC_API_KEY = [Net.NetworkCredential]::new('', $k).Password`.")
    if "ollama" in used:
        host = (llm.get("ollama") or {}).get("host", "http://localhost:11434")
        model = (llm.get("ollama") or {}).get("model", "")
        pulled = ollama_models(host)
        if pulled is None:
            hint = ("Ollama is installed but not running: open the Ollama app, or run `ollama serve`." if shutil.which("ollama")
                    else "Ollama is not installed. Get it from https://ollama.com/download, or move every stage to anthropic on the Models page.")
            add(FAIL, f"Ollama is not reachable at {host}. {hint}")
        elif model and not has_model(pulled, model):
            add(FAIL, f"Ollama is running but model '{model}' (from settings.yaml) is not pulled. Run `ollama pull {model}`.")
        else:
            add(OK, f"Ollama is running; model '{model}' is available")
    if not used:
        add(WARN, "no model stage configured")

    if not os.environ.get("APPLYBOT_MAIL_PASS"):
        add(INFO, "APPLYBOT_MAIL_PASS is not set. Only needed for sending mail and checking replies.")
    return out


def report(cfg: Config) -> int:
    mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", INFO: "    "}
    items = checks(cfg)
    for level, msg in items:
        print(f"[{mark[level]}] {msg}")
    fails = sum(1 for lv, _ in items if lv == FAIL)
    warns = sum(1 for lv, _ in items if lv == WARN)
    print("\n" + ("Ready to go: run `applybot serve --open`." if not fails and not warns else
                  f"{fails} problem(s) and {warns} warning(s) above. Fix the FAIL lines first." if fails else
                  f"{warns} warning(s) above; you can still start with `applybot serve --open`."))
    return 1 if fails else 0
