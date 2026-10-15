"""Shared timestamp formatting, independent of browser dependencies."""
from datetime import datetime


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")
