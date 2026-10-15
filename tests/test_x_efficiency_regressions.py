"""Synthetic invariants for CPU/I/O optimizations; no X requests or accounts."""
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import x_keyword_collector as c
from x_workbench.progress import RowDeltaTracker
from x_workbench.metrics import StageTimings, timing_scope, measure
from x_workbench.storage import write_json_atomic


def test_delta_scans_initial_prefix_once_not_per_page():
    rows = [{"post_id": str(i)} for i in range(10000)]
    tracker = RowDeltaTracker()
    scanned = 0
    for page in range(20):
        rows.extend({"post_id": str(len(rows) + i)} for i in range(20))
        scanned += len(tracker.pending(rows))
        tracker.acknowledge(rows)
    assert scanned == 10400
    assert sum(10000 + 20 * (page + 1) for page in range(20)) == 204200


def test_delta_retry_replacement_scans_again_and_unacknowledged_rows_remain():
    tracker = RowDeltaTracker()
    rows = [{"post_id": "1"}]
    assert tracker.pending(rows) == rows
    assert tracker.pending(rows) == rows  # Failed save: do not advance frontier.
    tracker.acknowledge(rows)
    assert tracker.pending(rows) == []
    retry_rows = list(rows)
    assert tracker.pending(retry_rows) == rows  # New accumulation on retry.
    tracker.acknowledge(retry_rows)
    retry_rows.clear()
    with pytest.raises(ValueError, match="shrank"):
        tracker.pending(retry_rows)


def test_empty_tail_still_commits_metadata_not_only_new_posts():
    tracker = RowDeltaTracker()
    rows = [{"post_id": "1"}]
    saved = []
    for page in range(3):
        new = tracker.pending(rows)
        saved.append({"page": page + 1, "new": len(new), "cursor": "c" + str(page + 1)})
        tracker.acknowledge(rows)
    assert [row["new"] for row in saved] == [1, 0, 0]
    assert saved[-1]["cursor"] == "c3" and len(saved) == 3


def test_exclusive_timing_does_not_double_count_children():
    ticks = iter([0.0, 2.0, 5.0, 10.0])
    timings = StageTimings(clock=lambda: next(ticks))
    with timing_scope(timings):
        with measure("checkpoint_write"):
            with measure("json_serialize"):
                pass
    stages = timings.snapshot()["stages"]
    assert stages["checkpoint_write"]["seconds"] == 10
    assert stages["checkpoint_write"]["self_seconds"] == 7
    assert stages["json_serialize"]["self_seconds"] == 3


def test_digest_is_of_exact_durable_bytes_and_no_reread(tmp_path, monkeypatch):
    path = tmp_path / "response.json"
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (_ for _ in ()).throw(AssertionError("unnecessary readback")))
    digest = write_json_atomic(path, {"中文": "gpt\ntext"}, return_digest=True)
    assert digest == hashlib.sha256(read(path)).hexdigest()
    assert json.loads(path.read_text(encoding="utf-8")) == {"中文": "gpt\ntext"}


def test_serialization_failure_keeps_old_file_and_cleans_temporary(tmp_path):
    path = tmp_path / "state.json"
    write_json_atomic(path, {"cursor": "old"})
    with pytest.raises(TypeError):
        write_json_atomic(path, {"bad": object()}, return_digest=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"cursor": "old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_page_status_builds_each_queue_once(tmp_path, monkeypatch):
    repair_calls, profile_calls = [], []
    repair = c.search_repair_queue
    profile = c.profile_location_queue
    monkeypatch.setattr(c, "search_repair_queue", lambda *args: repair_calls.append(1) or repair(*args))
    monkeypatch.setattr(c, "profile_location_queue", lambda *args: profile_calls.append(1) or profile(*args))
    payload = {"posts": [], "search_runs": [], "authors": {}, "repair_policy": "coverage_first"}
    c.save_checkpoint(tmp_path, payload)
    assert len(repair_calls) == len(profile_calls) == 1
    assert json.loads((tmp_path / "raw.json").read_text(encoding="utf-8")) == payload


def test_continuation_uses_one_response_archive_per_page(tmp_path, monkeypatch):
    # Reuse the synthetic browser fixture; never launch Chrome or contact X.
    from test_x_cursor_continuation import test_page_100_continues_101_preflight_not_added
    test_page_100_continues_101_preflight_not_added(tmp_path, monkeypatch)
    manifest_path = next(tmp_path.glob("search_end_evidence/*/responses_manifest.json"))
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    proof = json.loads((manifest_path.parent / "verification.json").read_text(encoding="utf-8"))
    assert len(entries) == 4 and len(proof["search_end_evidence"]["responses"]) == 3
    assert len(list(manifest_path.parent.glob("continuation_page_*.json"))) == 4
    assert not list(manifest_path.parent.glob("page_*.json"))
    assert not (manifest_path.parent / "final_response.json").exists()
    assert c.search_end_verified(proof)  # Final disk hash validation still performed.


def test_initial_response_is_not_parsed_twice(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from urllib.parse import urlencode
    day = date(2026, 6, 25)
    query = c.build_search_query("gpt", day, day)
    body = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": []}}}}}
    response = SimpleNamespace(status=200, url="https://x.com/i/api/graphql/h/SearchTimeline?" + urlencode({"variables": json.dumps({"rawQuery": query})}),
        request=SimpleNamespace(method="GET", post_data=None, all_headers=lambda: {}), header_value=lambda name: "", json=lambda: body)
    class Pending:
        value = response
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Page:
        def expect_response(self, *args, **kwargs): return Pending()
        def goto(self, *args, **kwargs): pass
    calls = []
    monkeypatch.setattr(c, "parse_search_timeline", lambda *args: calls.append(1) or ([{"post_id": "1", "posted_at": "2026-06-25T00:00:00Z"}], "next"))
    rows, result = c.collect_search_graphql(Page(), "gpt", 1, 0, 1, tmp_path, day, day)
    assert len(calls) == 1 and len(rows) == 1 and result["stop_reason"] == "max_pages"
