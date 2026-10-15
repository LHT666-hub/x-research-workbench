"""Bounded process-local evidence cache for live status only.

Default policy verification never uses a cache. Explicit status scopes may reuse
validated JSON while identity/size/mtime/ctime stay unchanged. Final acceptance
and resume validation must remain outside those scopes and rehash actual bytes.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

from .metrics import timed

_active = ContextVar("x_research_evidence_cache", default=None)


def _fingerprint(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class EvidenceCache:
    def __init__(self, max_entries=4096, max_bytes=32 * 1024 * 1024):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._items = OrderedDict()
        self._bytes = 0
        self.disk_reads = 0
        self.cache_hits = 0

    def read_json(self, path, digest):
        path = Path(path).resolve()
        stamp = _fingerprint(path)  # Missing/deleted files never hit the cache.
        key = (str(path), digest)
        previous = self._items.get(key)
        if previous is not None and previous[0] == stamp:
            self._items.move_to_end(key)
            self.cache_hits += 1
            return previous[1]
        if previous is not None:
            self._bytes -= self._items.pop(key)[2]
        self.disk_reads += 1
        raw = path.read_bytes()
        if stamp != _fingerprint(path):
            raise ValueError("Evidence changed during verification")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Evidence digest mismatch")
        value = json.loads(raw)
        if self.max_entries > 0 and len(raw) <= self.max_bytes:
            self._items[key] = (stamp, value, len(raw))
            self._bytes += len(raw)
            while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
                self._bytes -= self._items.popitem(last=False)[1][2]
        return value


@contextmanager
def evidence_cache_scope(cache):
    token = _active.set(cache)
    try:
        yield
    finally:
        _active.reset(token)


@timed("evidence_verify")
def evidence_payload(item):
    cache = _active.get()
    if cache is not None:
        return cache.read_json(item["response_file"], item["sha256"])
    raw = Path(item["response_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Evidence digest mismatch")
    return json.loads(raw)
