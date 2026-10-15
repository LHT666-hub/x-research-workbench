"""All fixtures synthetic; never log in, launch Chrome or request X."""
import hashlib
import json
import os
from pathlib import Path
from datetime import date
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import x_keyword_collector as c
from x_workbench import query, parsing, policy, storage
from x_workbench.evidence import EvidenceCache, evidence_cache_scope
from x_workbench.metrics import StageTimings, measure, timing_scope, current_timings
from x_workbench.http_transport import DirectHttpSearchTransport, HttpTransportError


def end_record(tmp_path):
    path = tmp_path / "end.json"
    storage.write_json_atomic(path, {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
        "instructions": [{"type": "TimelineTerminateTimeline", "direction": "Bottom"}]
    }}}}})
    return {"query": "gpt since_time:1 until_time:2", "count": 20, "stop_reason": "cursor_exhausted",
            "search_end_evidence": {"policy": "response_end_v1", "responses": [{
                "http_status": 200, "query": "gpt since_time:1 until_time:2", "request_cursor_matches": True,
                "next_cursor": "", "response_file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
            }]}}


def state(count=2, cursor="c1"):
    return {"posts": [{"post_id": str(i), "post_text": "synthetic"} for i in range(count)],
            "last_cursor": cursor, "plan_sha256": "synthetic-plan", "guard_hold": None}


def test_facade_keeps_compatible_public_functions():
    assert c.build_search_query is query.build_search_query
    assert c.parse_search_timeline is parsing.parse_search_timeline
    assert c.classify_search_run is policy.classify_search_run
    assert c._write_json_atomic is storage.write_json_atomic


def test_pure_components_do_not_require_browser_dependency():
    code = """
import sys
class BlockBrowser:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('playwright'):
            raise AssertionError('Browser dependency imported')
sys.meta_path.insert(0, BlockBrowser())
sys.path.insert(0, sys.argv[1])
from x_workbench import query, parsing, policy, storage, evidence, metrics, http_transport
assert 'playwright' not in sys.modules
print('pure components imported without browser')
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(TOOLS)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_status_cache_reads_once_but_final_acceptance_rehashes(tmp_path, monkeypatch):
    record = end_record(tmp_path)
    cache = EvidenceCache()
    with evidence_cache_scope(cache):
        assert policy.search_end_verified(record)
        assert policy.search_end_verified(record)
    assert cache.disk_reads == 1 and cache.cache_hits == 1
    calls = []
    read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: calls.append(path) or read(path))
    assert policy.search_end_verified(record)
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["rewrite", "delete", "guard", "query", "hash"])
def test_cached_end_does_not_survive_changes(tmp_path, change):
    record = end_record(tmp_path)
    item = record["search_end_evidence"]["responses"][0]
    path = Path(item["response_file"])
    cache = EvidenceCache()
    with evidence_cache_scope(cache):
        assert policy.search_end_verified(record)
        if change == "rewrite":
            path.write_text("{}", encoding="utf-8")
        elif change == "delete":
            path.unlink()
        elif change == "guard":
            record["guard_hold"] = {"reason": "hold"}
        elif change == "query":
            record["query"] = "different-query"
        else:
            item["sha256"] = "0" * 64
        assert not policy.search_end_verified(record)


def test_cache_is_bounded_and_same_size_rewrite_invalidates(tmp_path):
    cache = EvidenceCache(max_entries=1)
    paths = [tmp_path / f"{i}.json" for i in range(2)]
    for path in paths:
        path.write_text('{"v":1}', encoding="utf-8")
        assert cache.read_json(path, hashlib.sha256(path.read_bytes()).hexdigest()) == {"v": 1}
    assert len(cache._items) == 1
    digest = hashlib.sha256(paths[-1].read_bytes()).hexdigest()
    paths[-1].write_text('{"v":2}', encoding="utf-8")
    stamp = paths[-1].stat().st_mtime_ns + 2_000_000_000
    os.utime(paths[-1], ns=(stamp, stamp))
    with pytest.raises(ValueError, match="digest"):
        cache.read_json(paths[-1], digest)


def test_timing_counts_failure_and_restores_scope():
    clock = iter([1.0, 2.5, 5.0, 8.0])
    timings = StageTimings(clock=lambda: next(clock))
    with timing_scope(timings):
        with measure("parse"):
            pass
        with pytest.raises(RuntimeError):
            with measure("request_wait"):
                raise RuntimeError("synthetic")
    result = timings.snapshot()
    assert result["stages"]["parse"]["seconds"] == 1.5
    assert result["stages"]["request_wait"]["errors"] == 1
    assert result["stages"]["request_wait"]["seconds"] == 3.0
    assert current_timings() is None
    with pytest.raises(ValueError):
        with timings.measure("arbitrary-secret-label"):
            pass


def test_json_fsync_failure_keeps_previous_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    storage.write_json_atomic(path, {"cursor": "old"})
    before = path.read_bytes()
    monkeypatch.setattr(storage.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        storage.write_json_atomic(path, {"cursor": "new"})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_json_atomic_windows_permission_retry(tmp_path, monkeypatch):
    replace = Path.replace
    attempts = []
    def flaky(path, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("synthetic sharing violation")
        return replace(path, target)
    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(storage.time, "sleep", lambda seconds: None)
    path = tmp_path / "state.json"
    storage.write_json_atomic(path, {"正文": "gpt"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"正文": "gpt"}
    assert len(attempts) == 3


def test_sqlite_appends_only_new_rows_and_resumes_past_json_snapshot(tmp_path):
    path = tmp_path / "continuation_raw.json"
    initial = state(1000)
    storage.write_json_atomic(path, initial)
    original = path.read_bytes()
    store = storage.SQLiteCheckpoint(path)
    try:
        store.commit(initial)
        statements = []
        store.connection.set_trace_callback(statements.append)
        latest = state(1020, "c2")
        store.commit(latest)
        assert sum(sql.startswith("INSERT INTO posts") for sql in statements) == 20
        assert path.read_bytes() == original  # No cumulative rewrite per page.
    finally:
        store.close()
    restored = storage.load_continuation_state(path)
    assert restored == latest and len(restored["posts"]) == 1020


def test_sqlite_failed_transaction_never_advances_cursor_or_posts(tmp_path):
    path = tmp_path / "continuation_raw.json"
    store = storage.SQLiteCheckpoint(path)
    try:
        store.commit(state())
        store.connection.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON checkpoint BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END")
        store.connection.commit()
        with pytest.raises(OSError, match="transaction"):
            store.commit(state(3, "uncommitted-cursor"))
        assert store.load() == state()
        assert store.count == 2 and store.generation == 1
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [("guard_hold", {"reason": "manual-hold"}), ("cooldown_until_epoch", 9999999999)])
def test_sqlite_protection_and_cooling_survive_crash(tmp_path, field, value):
    path = tmp_path / "continuation_raw.json"
    store = storage.SQLiteCheckpoint(path)
    try:
        store.commit(state())
        store.export_snapshot(state())
        held = {**state(), field: value}
        store.commit(held)
    finally:
        store.close()
    assert storage.load_continuation_state(path)[field] == value


def test_sqlite_does_not_bypass_manually_changed_json_guard(tmp_path):
    path = tmp_path / "continuation_raw.json"
    store = storage.SQLiteCheckpoint(path)
    try:
        store.commit(state())
        store.export_snapshot(state())
        storage.write_json_atomic(path, {**state(), "guard_hold": {"reason": "manual-hold"}})
        with pytest.raises(ValueError, match="snapshot changed"):
            store.load()
        with pytest.raises(ValueError, match="snapshot changed"):
            store.export_snapshot(state())
    finally:
        store.close()


def test_sqlite_rejects_shrink_and_uncommitted_snapshot_metadata(tmp_path):
    store = storage.SQLiteCheckpoint(tmp_path / "continuation_raw.json")
    try:
        store.commit(state())
        with pytest.raises(ValueError, match="remove"):
            store.commit(state(1))
        with pytest.raises(ValueError, match="metadata"):
            store.export_snapshot(state(cursor="not-committed"))
    finally:
        store.close()


def test_sqlite_detects_missing_post_rows(tmp_path):
    store = storage.SQLiteCheckpoint(tmp_path / "continuation_raw.json")
    try:
        store.commit(state())
        store.connection.execute("DELETE FROM posts WHERE ordinal=0")
        store.connection.commit()
        with pytest.raises(ValueError, match="inconsistency"):
            store.load()
    finally:
        store.close()


def test_legacy_json_reader_needs_no_sqlite(tmp_path):
    path = tmp_path / "continuation_raw.json"
    storage.write_json_atomic(path, state())
    assert storage.load_continuation_state(path) == state()
    assert not path.with_suffix(".sqlite3").exists()


def url(cursor=""):
    return "https://x.com/i/api/graphql/test/SearchTimeline?" + urlencode({"variables": json.dumps({"rawQuery": "gpt since_time:1 until_time:2", "cursor": cursor, "product": "Latest"})})


class Client:
    def __init__(self, status=200, response_url=None):
        self.calls, self.status, self.response_url = [], status, response_url
    def request(self, method, request_url, **kwargs):
        self.calls.append((method, request_url, kwargs))
        return SimpleNamespace(status_code=self.status, url=self.response_url or request_url,
                               headers={"x-rate-limit-reset": "123456", "authorization": "DO-NOT-EXPOSE"}, json=lambda: {"status_fixture": self.status})


def test_http_is_explicitly_disabled_by_default():
    client = Client()
    transport = DirectHttpSearchTransport(url(), "gpt since_time:1 until_time:2", client, lambda *args: {})
    with pytest.raises(HttpTransportError, match="disabled"):
        transport.fetch("c1")
    assert not client.calls


@pytest.mark.parametrize("status", [200, 401, 403, 404, 429, 500])
def test_http_preserves_errors_and_reset_without_retry(status):
    client = Client(status)
    transport = DirectHttpSearchTransport(url(), "gpt since_time:1 until_time:2", client,
                                          lambda *args: {"x-client-transaction-id": "synthetic-signature"}, enable_network=True)
    response = transport.fetch("c1")
    assert response.status == status and response.json() == {"status_fixture": status}
    assert response.header_value("x-rate-limit-reset") == "123456"
    assert response.header_value("authorization") == ""
    assert len(client.calls) == 1 and client.calls[0][2]["follow_redirects"] is False
    assert "synthetic-signature" not in repr(response)


@pytest.mark.parametrize("target", ["https://evil.invalid/i/api/graphql/test/SearchTimeline", "http://x.com/i/api/graphql/test/SearchTimeline", "https://x.com/i/api/graphql/test/AboutAccountQuery"])
def test_http_rejects_unapproved_endpoints(target):
    with pytest.raises(ValueError):
        DirectHttpSearchTransport(target, "q", Client(), lambda *args: {})


def test_http_request_cursor_and_fresh_signing_each_time():
    client, signatures = Client(), []
    def sign(*args):
        signatures.append(args)
        return {"x-client-transaction-id": "synthetic-" + str(len(signatures))}
    transport = DirectHttpSearchTransport(url(), "gpt since_time:1 until_time:2", client, sign, enable_network=True)
    transport.fetch("page-100")
    transport.fetch("page-101")
    assert len(signatures) == 2
    assert c._search_request_cursor(client.calls[0][1]) == "page-100"
    assert c._search_request_cursor(client.calls[1][1]) == "page-101"


def test_http_mismatched_response_not_accepted():
    transport = DirectHttpSearchTransport(url(), "gpt since_time:1 until_time:2", Client(response_url=url("wrong")),
                                          lambda *args: {"x-client-transaction-id": "synthetic"}, enable_network=True)
    with pytest.raises(HttpTransportError, match="mismatch"):
        transport.fetch("expected")


def timeline(post_id, cursor):
    entries = []
    if post_id:
        entries.append({"content": {"itemContent": {"tweet_results": {"result": {
            "rest_id": post_id, "legacy": {"full_text": "synthetic gpt", "created_at": "Thu Jun 25 00:00:00 +0000 2026"},
            "core": {"user_results": {"result": {"rest_id": "1", "legacy": {"screen_name": "synthetic", "location": ""}}}}
        }}}}})
    if post_id:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
        instructions = [{"type": "TimelineAddEntries", "entries": entries}]
    else:
        instructions = [{"type": "TimelineReplaceEntry", "entry": {"content": {"cursorType": "Bottom", "value": cursor}}}]
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": instructions}}}}}


def test_browser_and_http_share_end_policy_and_id_set_offline(tmp_path, monkeypatch):
    day = date(2026, 6, 25)
    exact_query = c.build_search_query("gpt", day, day)
    bodies = [timeline("1", "c1"), timeline("2", "c2"), timeline(None, "c3"), timeline(None, "c4"), timeline(None, "c5")]
    def request_url(cursor):
        return "https://x.com/i/api/graphql/test/SearchTimeline?" + urlencode({"variables": json.dumps({"rawQuery": exact_query, "cursor": cursor, "product": "Latest"})})
    browser_responses = [SimpleNamespace(status=200, url=request_url("" if i == 0 else f"c{i}"),
        request=SimpleNamespace(post_data=None, method="GET", all_headers=lambda: {}),
        header_value=lambda name: "", json=lambda body=body: body) for i, body in enumerate(bodies)]
    class Page:
        def expect_response(self, *args, **kwargs):
            class Pending:
                value = browser_responses[0]
                def __enter__(self): return self
                def __exit__(self, *args): return False
            return Pending()
        def goto(self, *args, **kwargs): pass
    following = iter(browser_responses[1:])
    monkeypatch.setattr(c, "_next_browser_signed_search_response", lambda *args, **kwargs: (next(following), 1))
    monkeypatch.setattr(c.time, "sleep", lambda seconds: None)
    browser_rows, browser_end = c.collect_search_graphql(Page(), "gpt", 1, 0, 10, tmp_path / "browser", day, day, no_growth_stop=8)
    class HttpClient:
        def request(self, method, requested_url, **kwargs):
            cursor = c._search_request_cursor(requested_url)
            index = int(cursor[1:]) if cursor else 0
            return SimpleNamespace(status_code=200, url=requested_url, headers={}, json=lambda: bodies[index])
    transport = DirectHttpSearchTransport(request_url(""), exact_query, HttpClient(),
        lambda *args: {"x-client-transaction-id": "synthetic"}, enable_network=True)
    http_rows, http_end = c.collect_search_http(transport, "gpt", 1, 0, 10, tmp_path / "http", day, day, no_growth_stop=8)
    assert {row["post_id"] for row in browser_rows} == {row["post_id"] for row in http_rows} == {"1", "2"}
    for end in (browser_end, http_end):
        assert end["json_pages_used"] == 5 and end["date_rejected_count"] == 0
        assert end["state_code"] == "validated_empty_tail" and c.search_end_verified(end)
        assert not end["platform_all_posts_proven"]
    assert http_end["request_transport"] == "experimental_direct_http_json"
    assert json.loads((tmp_path / "http/timings.json").read_text(encoding="utf-8"))["stages"]["request_wait"]["calls"] == 5


@pytest.mark.parametrize("backend", ["json", "sqlite"])
@pytest.mark.parametrize("cooling", [False, True])
def test_continuation_runner_checkpoint_integration_without_browser(tmp_path, monkeypatch, backend, cooling):
    import x_continue_saved_cursors as runner
    source_path = tmp_path / "historical.json"
    cfg_path, plan_path = tmp_path / "config.json", tmp_path / "plan.json"
    day = "2026-06-25"
    old_post = {"post_id": "1", "posted_at": day + "T00:00:00Z", "post_text": "synthetic gpt"}
    storage.write_json_atomic(source_path, {"posts": [old_post]})
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    cfg = {"account_node": "a", "repair_policy": "coverage_first", "search_engine": "graphql", "author_strategy": "deferred",
           "timezone_offset": 8, "search_type": "live", "retry_limit": 0, "max_scrolls": 1000, "no_growth_stop": 8,
           "graphql_delay_min": 2, "graphql_delay_max": 4, "delay_min": 2, "delay_max": 4,
           "break_every": 10, "break_min": 60, "break_max": 120}
    storage.write_json_atomic(cfg_path, cfg)
    item = {"keyword": "gpt", "slice_start": day, "source_path": str(source_path), "source_record_index": 0,
            "query": c.build_search_query("gpt", date.fromisoformat(day), date.fromisoformat(day)),
            "json_pages_used": 100, "last_cursor": "saved-100", "source_attempt_count": 20}
    storage.write_json_atomic(plan_path, {"account_node": "a", "run_id": "synthetic", "config_path": str(cfg_path),
        "config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(), "queue": [item],
        "sources": [{"path": str(source_path), "sha256": source_sha, "manual_pause_verified": True}]})
    waited = []
    if cooling:
        storage.write_json_atomic(tmp_path / "collection/continuation_raw.json", {
            "posts": [], "search_runs": [], "guard_hold": None, "finalized": False,
            "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            "cooldown_until_epoch": runner.time.time() + 300,
        })
    monkeypatch.setattr(runner, "measured_sleep", lambda seconds, stage="pacing_sleep": waited.append((seconds, stage)))
    monkeypatch.setattr(runner, "account_busy", lambda *args: "")
    monkeypatch.setattr(runner, "manual_pause_verified", lambda *args: True)
    monkeypatch.setattr(c, "ROOT", tmp_path)
    class Context:
        pages = [object()]
        def cookies(self): return [{"name": "auth_token"}]
        def close(self): pass
    class Playwright:
        chromium = SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: Context())
        def __enter__(self):
            if cooling:
                assert waited and waited[0][0] >= 290 and waited[0][1] == "backoff_sleep"
            return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(c, "sync_playwright", Playwright)
    def collect(*args, **kwargs):
        rows = [old_post, {**old_post, "post_id": "2"}]
        for page in (101, 102, 103):
            if page == 102:
                rows.append({**old_post, "post_id": "3"})
            progress = {**item, "json_pages_used": page, "last_cursor": f"saved-{page}", "count": len(rows),
                        "parsed_post_count": len(rows) - 1, "date_rejected_count": 0, "stop_reason": "in_progress"}
            kwargs["checkpoint"](rows, progress)
            saved = storage.load_continuation_state(tmp_path / "collection/continuation_raw.json")
            assert saved["current_progress"]["page"] == page
            assert len(saved["posts"]) == (1 if page == 101 else 2)
        assert saved["search_runs"][0]["last_cursor"] == "saved-103"  # Empty new tail still durable.
        return [], {**progress, "stop_reason": "max_pages", "coverage_status": "truncated", "state_code": "max_pages"}
    monkeypatch.setattr(c, "collect_search_graphql", collect)
    monkeypatch.setattr(sys, "argv", ["runner", "--account-node", "a", "--run-id", "synthetic", "--plan", str(plan_path),
        "--search-engine", "graphql", "--author-strategy", "deferred", "--checkpoint-backend", backend])
    runner.main()
    result = json.loads((tmp_path / "collection/continuation_raw.json").read_text(encoding="utf-8"))
    assert [post["post_id"] for post in result["posts"]] == ["2", "3"]
    assert not result["finalized"] and not result["guard_hold"]
    assert not (tmp_path / "collection/worker.lock").exists()
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_sha
    assert (tmp_path / "collection/timings.json").exists()
