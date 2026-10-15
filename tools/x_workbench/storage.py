"""Atomic JSON compatibility and opt-in append-only continuation checkpoints."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .metrics import measure


@contextmanager
def database_write(connection):
    try:
        with connection:
            yield
    except sqlite3.Error as exc:
        # Storage errors must never enter HTTP/network retry handling.
        raise OSError("Checkpoint database transaction failed") from exc


def write_json_atomic(path, value, *, return_digest=False):
    """Unique temp + flush/fsync + atomic replacement; never truncate target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = "evidence_write" if "search_end_evidence" in path.parts or "search_failure_evidence" in path.parts else "checkpoint_write"
    with measure(stage):
        descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            # Own the descriptor before serializing so failures close it on Windows.
            with os.fdopen(descriptor, "wb") as stream:
                with measure("json_serialize"):
                    serialized = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(8):
                try:
                    temporary.replace(path)
                    break
                except PermissionError:
                    if attempt >= 7:
                        raise
                    time.sleep(0.12 * (attempt + 1))
            return hashlib.sha256(serialized).hexdigest() if return_digest else None
        finally:
            temporary.unlink(missing_ok=True)


class SQLiteCheckpoint:
    """Single-owner append-only posts + mutable metadata in one FULL transaction.

    A compatibility JSON snapshot is exported explicitly, not on every page.
    The runner owns the posts list: existing committed posts must not be edited.
    This backend is opt-in; it never imports or mutates original batch data.
    """
    def __init__(self, snapshot_path, *, read_only=False):
        self.snapshot_path = Path(snapshot_path).resolve()
        self.path = self.snapshot_path.with_suffix(".sqlite3")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5) if read_only else sqlite3.connect(self.path, timeout=5)
        try:
            if not read_only:
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("CREATE TABLE IF NOT EXISTS posts (ordinal INTEGER PRIMARY KEY, body TEXT NOT NULL)")
                self.connection.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, post_count INTEGER NOT NULL, state TEXT NOT NULL, snapshot_sha TEXT)")
                self.connection.commit()
            self.count = self.connection.execute("SELECT count(*) FROM posts").fetchone()[0]
            row = self.connection.execute("SELECT generation FROM checkpoint WHERE id=1").fetchone()
            if row is None and self.count:
                raise ValueError("Orphan checkpoint posts; manual review required")
            self.generation = row[0] if row else 0
        except BaseException:
            self.connection.close()
            raise

    def load(self):
        # A live writer may commit concurrently. Read metadata and posts from
        # the same WAL snapshot, never combine cursor N with rows N+1.
        self.connection.execute("BEGIN")
        try:
            return self._load_snapshot()
        finally:
            self.connection.rollback()

    def _load_snapshot(self):
        row = self.connection.execute("SELECT state,snapshot_sha,post_count FROM checkpoint WHERE id=1").fetchone()
        if row is None:
            return None
        state = json.loads(row[0])
        actual_count, minimum, maximum = self.connection.execute("SELECT count(*),min(ordinal),max(ordinal) FROM posts").fetchone()
        if row[2] != actual_count or (actual_count and (minimum != 0 or maximum != actual_count - 1)):
            raise ValueError("Checkpoint row count/ordinal inconsistency")
        # Manual JSON guard edits must not be silently bypassed by the sidecar.
        if row[1]:
            if not self.snapshot_path.exists() or hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest() != row[1]:
                raise ValueError("Compatibility snapshot changed; review before SQLite resume")
        elif self.snapshot_path.exists():
            raise ValueError("Unanchored compatibility snapshot; review required")
        state["posts"] = [json.loads(record[0]) for record in self.connection.execute("SELECT body FROM posts ORDER BY ordinal")]
        return state

    def commit(self, payload):
        posts = payload["posts"]
        if len(posts) < self.count:
            raise ValueError("Append-only checkpoint cannot remove posts")
        metadata = {key: value for key, value in payload.items() if key != "posts"}
        # Serialize before BEGIN: any serialization failure cannot advance cursor.
        with measure("json_serialize"):
            additions = [(index, json.dumps(posts[index], ensure_ascii=False, separators=(",", ":")))
                         for index in range(self.count, len(posts))]
            state = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        generation = self.generation + 1
        anchor = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest() if not self.generation and self.snapshot_path.exists() else None
        with measure("checkpoint_write"), database_write(self.connection):
            self.connection.executemany("INSERT INTO posts(ordinal,body) VALUES (?,?)", additions)
            self.connection.execute("INSERT INTO checkpoint(id,generation,post_count,state,snapshot_sha) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,post_count=excluded.post_count,state=excluded.state", (generation, len(posts), state, anchor))
        self.count, self.generation = len(posts), generation

    def export_snapshot(self, payload):
        if len(payload["posts"]) != self.count:
            raise ValueError("Commit posts and cursor before exporting")
        saved = self.connection.execute("SELECT state,snapshot_sha FROM checkpoint WHERE id=1").fetchone()
        if saved is None or json.loads(saved[0]) != {key: value for key, value in payload.items() if key != "posts"}:
            raise ValueError("Commit metadata before exporting")
        if saved[1] and (not self.snapshot_path.exists() or hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest() != saved[1]):
            raise ValueError("Compatibility snapshot changed; do not overwrite manual protection")
        write_json_atomic(self.snapshot_path, payload)
        digest = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()
        with database_write(self.connection):
            self.connection.execute("UPDATE checkpoint SET snapshot_sha=? WHERE id=1", (digest,))

    def close(self):
        self.connection.close()


def load_continuation_state(path):
    """Read authoritative sidecar if present, otherwise legacy JSON; no network."""
    path = Path(path).resolve()
    if path.with_suffix(".sqlite3").exists():
        store = SQLiteCheckpoint(path, read_only=True)
        try:
            state = store.load()
            if state is not None:
                return state
        finally:
            store.close()
    return json.loads(path.read_text(encoding="utf-8"))
