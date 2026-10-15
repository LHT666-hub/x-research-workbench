"""Local runtime discovery and diagnostics. Never logs in or contacts X."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Mapping


def discover_chrome(
    environment: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
) -> Path:
    """An explicit override is authoritative, including when it is invalid."""
    env = os.environ if environment is None else environment
    configured = env.get("X_WORKBENCH_CHROME", "").strip().strip('"')
    if configured:
        return Path(configured).expanduser()
    candidates = [
        Path(env.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    ]
    if env.get("LOCALAPPDATA"):
        candidates.append(Path(env["LOCALAPPDATA"]) / "Google/Chrome/Application/chrome.exe")
    for path in candidates:
        if path.is_file():
            return path
    lookup = shutil.which if which is None else which
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = lookup(name)
        if found and Path(found).is_file():
            return Path(found)
    return candidates[0]


def diagnose_runtime(root: Path) -> dict:
    """Read-only checks: no browser, output files, cookies or network access."""
    from . import __version__

    checks = []

    def check(name: str, ok: bool, message: str, remedy: str = "") -> None:
        checks.append({"name": name, "level": "ok" if ok else "error",
                       "message": message, "remedy": "" if ok else remedy})

    check("python", sys.version_info >= (3, 11),
          f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
          "Install Python 3.11+ and recreate this project's .venv.")
    check("playwright", importlib.util.find_spec("playwright") is not None,
          "Playwright Python package",
          "Run .venv\\Scripts\\python.exe -m pip install -r requirements.txt.")
    chrome = discover_chrome()
    check("chrome", chrome.is_file(), str(chrome),
          "Install Google Chrome, or set X_WORKBENCH_CHROME to the full executable path. An invalid override will not silently fall back.")
    required = ("tools/x_keyword_ui.py", "tools/x_keyword_collector.py", "requirements.txt")
    missing = [name for name in required if not (root / name).is_file()]
    check("project_files", not missing, "Required application files" if not missing else ", ".join(missing),
          "Download the complete repository; do not copy only one Python file.")
    check("project_writable", os.access(root, os.W_OK), "Project directory write permission",
          "Move the project to a writable personal folder; do not run inside Program Files.")
    try:
        free_bytes = shutil.disk_usage(root).free
        checks.append({"name": "disk_space", "level": "ok" if free_bytes >= 1024**3 else "warning",
                       "message": f"Free space: {free_bytes / 1024**3:.1f} GiB",
                       "remedy": "" if free_bytes >= 1024**3 else "Keep extra free space for raw responses and checkpoints."})
    except OSError as exc:
        checks.append({"name": "disk_space", "level": "warning",
                       "message": type(exc).__name__, "remedy": "Check that the project directory is accessible."})
    if sys.platform != "win32":
        checks.append({"name": "platform", "level": "warning",
                       "message": "Windows-first application; this platform is not fully validated.",
                       "remedy": "The UI launcher currently expects .venv/Scripts/python.exe."})
    return {"version": __version__, "ok": not any(c["level"] == "error" for c in checks),
            "checks": checks, "login_checked": False, "network_requests": 0,
            "note": "Environment checks do not prove login validity, X availability, collection speed or search completeness."}


def print_diagnostics(report: dict, *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"X Research Workbench {report['version']} - local environment checks")
        for item in report["checks"]:
            print(f"[{item['level'].upper()}] {item['name']}: {item['message']}")
            if item["remedy"]:
                print(f"  -> {item['remedy']}")
        print(report["note"])
    return 0 if report["ok"] else 1
