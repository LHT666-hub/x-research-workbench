"""One authoritative read entry for legacy JSON and incremental checkpoints."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading

from .clock import now_iso
from .storage import SQLiteCheckpoint

STATE_NAMES = {"raw.json", "continuation_raw.json", "supplement_raw.json"}


class DigestCache:
    """Only live views may cache file digests; audit/export/resume read afresh."""
    def __init__(self):
        self.items = OrderedDict()
        self.lock = threading.Lock()

    def digest(self, path):
        path = Path(path).resolve()
        info = path.stat()
        stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        with self.lock:
            previous = self.items.get(str(path))
            if previous and previous[0] == stamp:
                return previous[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        after = path.stat()
        if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Snapshot changed while checking its anchor")
        with self.lock:
            self.items[str(path)] = (stamp, digest)
            self.items.move_to_end(str(path))
            while len(self.items) > 64:
                self.items.popitem(last=False)
        return digest


LIVE_DIGESTS = DigestCache()


@dataclass
class StateSnapshot:
    state: dict
    backend: str
    revision: str
    post_count: int
    captured_at: str
    authoritative_path: str
    preview_only: bool = False

    def receipt(self):
        return {key: value for key, value in self.__dict__.items() if key != "state"}


def read_state(path, *, preview_limit=None, live=False):
    """Never fall back to stale JSON if an existing SQLite checkpoint is invalid.

    Each operation captures one revision. Later UI/export requests may observe a
    later committed revision, explicitly identified in their receipts.
    """
    path = Path(path).resolve()
    if preview_limit is not None and (not isinstance(preview_limit, int) or preview_limit < 0):
        raise ValueError("Invalid preview limit")
    if path.with_suffix(".sqlite3").exists():
        store = SQLiteCheckpoint(path, read_only=True)
        try:
            state = store.load(preview_limit=preview_limit, digest_cache=LIVE_DIGESTS if live else None)
            if state is None:
                raise ValueError("SQLite checkpoint has no committed state; no JSON fallback")
            return StateSnapshot(state, "sqlite", f"sqlite-{store.loaded_generation}", store.loaded_post_count,
                                 now_iso(), str(store.path), preview_limit is not None)
        finally:
            store.close()
    blob = path.read_bytes()
    state = json.loads(blob.decode("utf-8-sig"))
    if not isinstance(state, dict) or not isinstance(state.get("posts", []), list):
        raise ValueError("Invalid research checkpoint")
    state.setdefault("posts", [])
    count = len(state["posts"])
    if preview_limit is not None:
        state["posts"] = state["posts"][-preview_limit:] if preview_limit else []
    return StateSnapshot(state, "json", "json-" + hashlib.sha256(blob).hexdigest(), count,
                         now_iso(), str(path), preview_limit is not None)
