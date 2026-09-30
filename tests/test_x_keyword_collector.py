from __future__ import annotations

import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
sys.path.insert(0, str(PROJECT / "tools"))

import x_keyword_collector as collector  # noqa: E402
from x_account_collector import about_result  # noqa: E402


def test_extract_visible_post() -> None:
    markup = """
    <article data-testid="tweet">
      <div data-testid="User-Name">Alice Example\n@alice\n·</div>
      <div data-testid="tweetText">This is the original post text.</div>
      <a href="/alice/status/123456"><time datetime="2026-09-27T01:02:03.000Z">2h</time></a>
    </article>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(collector.CHROME), headless=True)
        page = browser.new_page()
        page.set_content(markup)
        assert collector.wait_for_search_ready(page, 1) == "posts"
        rows = collector.extract_visible_posts(page, "sample keyword", 1)
        page.set_content("<body>No results for sample keyword</body>")
        assert collector.wait_for_search_ready(page, 1) == "empty"
        browser.close()

    assert len(rows) == 1
    assert rows[0]["post_text"] == "This is the original post text."
    assert rows[0]["author_username"] == "alice"
    assert rows[0]["posted_at"] == "2026-09-27T01:02:03.000Z"
    assert rows[0]["post_id"] == "123456"


def test_real_about_response_path() -> None:
    evidence = WORKSPACE / "x_probe_logged_in" / "matched_response_08.json"
    if not evidence.exists():
        pytest.skip("local captured AboutAccountQuery fixture is intentionally not committed")
    capture = json.loads(evidence.read_text(encoding="utf-8"))
    payload = json.loads(capture["body"])
    result = about_result(payload)
    assert result is not None
    assert result["rest_id"] == "147929555"
    assert result["about_profile"]["account_based_in"] == "United States"


def test_retry_after_seconds_prefers_server_headers(monkeypatch) -> None:
    monkeypatch.setattr(collector.time, "time", lambda: 1_000.0)
    assert collector._retry_after_seconds({"retry-after": "120"}) == 120
    assert collector._retry_after_seconds({"x-rate-limit-reset": "1180"}) == 180
    assert collector._retry_after_seconds({}) is None


def test_historical_date_query_is_inclusive() -> None:
    query = collector.build_search_query("AI policy", date(2026, 1, 2), date(2026, 1, 5))
    assert query == "AI policy since:2026-01-02 until:2026-01-05"
    assert collector.post_in_date_range("2026-01-05T15:59:59Z", date(2026, 1, 2), date(2026, 1, 5), 8)
    assert not collector.post_in_date_range("2026-01-05T16:00:00Z", date(2026, 1, 2), date(2026, 1, 5), 8)
    assert collector.post_in_date_range("2025-12-31T16:00:00Z", date(2026, 1, 1), date(2026, 1, 1), 8)
    slices = collector.make_date_slices(date(2026, 1, 1), date(2026, 1, 10), 3)
    assert slices == [
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 1, 4), date(2026, 1, 6)),
        (date(2026, 1, 7), date(2026, 1, 9)),
        (date(2026, 1, 10), date(2026, 1, 10)),
    ]


def test_parse_search_timeline_keeps_stable_author_id_and_profile_location() -> None:
    payload = {
        "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [{"entries": [
            {
                "entryId": "tweet-123456",
                "content": {"itemContent": {"tweet_results": {"result": {
                    "rest_id": "123456",
                    "legacy": {"full_text": "GraphQL result text", "created_at": "Mon Sep 28 01:02:03 +0000 2026"},
                    "core": {"user_results": {"result": {
                        "rest_id": "777",
                        "core": {"screen_name": "alice", "name": "Alice"},
                        "location": {"location": "New York, USA"},
                        "legacy": {"location": "Legacy fallback"},
                    }}},
                }}}},
            },
            {"entryId": "cursor-bottom-0", "content": {"cursorType": "Bottom", "value": "NEXT-CURSOR"}},
        ]}]}}}},
    }
    rows, cursor = collector.parse_search_timeline(payload, "GPT", 1)
    assert cursor == "NEXT-CURSOR"
    assert len(rows) == 1
    assert rows[0]["post_id"] == "123456"
    assert rows[0]["author_username"] == "alice"
    assert rows[0]["author_user_id"] == "777"
    assert rows[0]["profile_location"] == "New York, USA"
    assert rows[0]["profile_location_status"] == "success"
    assert rows[0]["source"] == "search_timeline_graphql"
    assert rows[0]["account_lookup_status"] == "pending"


def test_register_pending_authors_is_durable_and_deduplicated() -> None:
    posts = [
        {"author_username": "Alice", "author_user_id": "777", "profile_location": "New York", "profile_location_collected_at": "now"},
        {"author_username": "alice", "author_user_id": "777", "profile_location": "New York", "profile_location_collected_at": "now"},
    ]
    authors: dict[str, dict[str, object]] = {}
    assert collector.register_pending_authors(posts, authors) == 1
    assert authors["alice"]["user_id"] == "777"
    assert authors["alice"]["profile_location"] == "New York"
    assert authors["alice"]["profile_location_status"] == "success"
    assert authors["alice"]["status"] == "pending"


def test_profile_location_json_parser_and_queue_are_independent_from_account_based_in() -> None:
    parsed = collector._profile_author_from_payload("alice", {
        "data": {"user": {"result": {
            "rest_id": "777", "core": {"screen_name": "Alice"},
            "location": {"location": "Boston, MA"},
        }}}
    })
    assert parsed["profile_location"] == "Boston, MA"
    assert parsed["profile_location_status"] == "success"

    authors = {
        "alice": {
            "username": "Alice", "user_id": "777", "profile_location": "",
            "profile_location_status": "pending", "account_based_in": "United States",
            "status": "success",
        },
        "bob": {
            "username": "bob", "profile_location": "", "profile_location_status": "empty",
            "account_based_in": "", "status": "pending",
        },
    }
    queue = collector.profile_location_queue(authors)
    assert [row["username"] for row in queue] == ["Alice"]
    posts = [{"author_username": "Alice", "account_based_in": "United States", "account_lookup_status": "success"}]
    authors["alice"].update(parsed)
    collector.enrich_profile_location_posts(posts, authors["alice"])
    assert posts[0]["profile_location"] == "Boston, MA"
    assert posts[0]["account_based_in"] == "United States"
    assert posts[0]["account_lookup_status"] == "success"


def test_direct_profile_url_rewrites_observed_variables() -> None:
    url = "https://x.com/i/api/graphql/hash/UserByScreenName?variables=%7B%22screen_name%22%3A%22old%22%7D"
    rewritten = collector._direct_profile_url(url, "Alice")
    variables = json.loads(collector.parse_qs(collector.urlsplit(rewritten).query)["variables"][0])
    assert variables["screen_name"] == "Alice"


def test_graphql_audit_metadata_excludes_credentials() -> None:
    operation, path = collector._graphql_operation(
        "https://x.com/i/api/graphql/temporary-hash/SearchTimeline?variables=%7B%7D"
    )
    assert operation == "SearchTimeline"
    assert path == "/i/api/graphql/temporary-hash/SearchTimeline"
    assert "variables" not in path


def test_search_template_does_not_replay_request_bound_transaction_id() -> None:
    class Request:
        method = "GET"
        post_data = None

        @staticmethod
        def all_headers() -> dict[str, str]:
            return {
                "authorization": "Bearer observed-session-token",
                "x-csrf-token": "observed-csrf",
                "x-client-transaction-id": "bound-to-the-original-url",
            }

    class Response:
        url = "https://x.com/i/api/graphql/hash/SearchTimeline?variables=%7B%7D"
        request = Request()

    template = collector._search_template(Response())
    assert "authorization" in template["headers"]
    assert "x-csrf-token" in template["headers"]
    assert "x-client-transaction-id" not in template["headers"]


def test_search_response_matcher_rejects_a_stale_date_slice() -> None:
    current = "GPT since:2026-01-08 until:2026-01-08"
    stale = "GPT since:2026-01-07 until:2026-01-07"

    class Request:
        post_data = None

    class Response:
        status = 200
        request = Request()

        def __init__(self, raw_query: str) -> None:
            variables = collector.quote(json.dumps({"rawQuery": raw_query}, separators=(",", ":")))
            self.url = f"https://x.com/i/api/graphql/hash/SearchTimeline?variables={variables}"

    assert collector._search_raw_query(Response(current).url) == current
    assert collector._search_response_matches(Response(current), current)
    assert not collector._search_response_matches(Response(stale), current)


def test_search_raw_query_supports_post_body() -> None:
    body = json.dumps({"variables": {"rawQuery": "Claude since:2026-01-01 until:2026-01-02"}})
    assert collector._search_raw_query("https://x.com/i/api/graphql/hash/SearchTimeline", body) == "Claude since:2026-01-01 until:2026-01-02"


def test_dedup_groups_and_one_click_exports(tmp_path: Path) -> None:
    first = {
        "keyword": "AI", "post_id": "99", "post_url": "https://x.com/alice/status/99",
        "post_text": "One post matching two terms", "posted_at": "2026-09-27T01:00:00Z",
        "time_label": "1h", "author_username": "alice", "author_display_name": "Alice",
        "author_user_id": "42", "account_based_in": "United States",
        "source": "about_this_account", "account_lookup_status": "success", "collected_at": "now",
    }
    second = dict(first, keyword="ChatGPT")
    payload = {"requested_keywords": ["AI", "ChatGPT"], "search_runs": [{"keyword": "AI", "keyword_index": 1, "query": "AI since:2026-09-27 until:2026-09-27", "slice_start": "2026-09-27", "slice_end": "2026-09-27", "count": 1, "scrolls_used": 7, "stop_reason": "no_new_results", "coverage_status": "apparently_exhausted", "completed_at": "now"}], "posts": [first, second], "authors": {}, "finalized": True}

    collector.save_outputs(tmp_path, payload)
    deduped = collector.deduplicate_posts(payload["posts"])
    assert len(deduped) == 1
    assert deduped[0]["matched_keywords"] == "AI | ChatGPT"
    for name in ("posts.csv", "posts_deduplicated.csv", "research_coding_template.csv", "country_summary.csv", "keyword_summary.csv", "author_summary.csv", "posted_date_summary.csv", "coverage_report.csv", "research_dataset.json", "profile_location_queue.json", "index.html", "all_exports.zip"):
        assert (tmp_path / name).exists()
    with zipfile.ZipFile(tmp_path / "all_exports.zip") as archive:
        assert "posts_deduplicated.csv" in archive.namelist()
        assert "research_coding_template.csv" in archive.namelist()
        assert "profile_location_queue.json" in archive.namelist()
    report = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "下载全部 ZIP" in report
    assert "账号所在地分布" in report
    assert "帖子时间分布" in report
    assert "搜索覆盖诊断" in report
    assert "滚动无新增" in report
    assert "耗时 / 实测速率" in report
    assert "id='authorSearch'" in report
    assert "id='locationStatus'" in report
    assert "id='sortOrder'" in report
    assert "id='pageSize'" in report
    assert "id='filteredCount'" in report
    coverage_header = (tmp_path / "coverage_report.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "operation_name" in coverage_header
    assert "endpoint_path" in coverage_header
    assert "posts_per_minute" in coverage_header
    assert "pagination_trigger_retries" in coverage_header
    assert "last_cursor" in coverage_header
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(collector.CHROME), headless=True)
        page = browser.new_page(accept_downloads=True)
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto((tmp_path / "index.html").as_uri())
        assert page_errors == []
        assert page.evaluate("typeof apply") == "function"
        page.select_option("#country", "united states")
        page.fill("#dateFrom", "2026-09-27")
        page.fill("#dateTo", "2026-09-27")
        assert page.locator("#rows tr:not([hidden])").count() == 1
        page.fill("#authorSearch", "alice")
        page.select_option("#locationStatus", "known")
        page.select_option("#pageSize", "25")
        assert page.locator("#filteredCount").inner_text() == "1"
        assert page.locator("#filteredAuthors").inner_text() == "1"
        with page.expect_download() as pending:
            page.click("#filteredExport")
        assert pending.value.suggested_filename == "filtered_posts.csv"
        browser.close()


def test_reuses_author_location_from_previous_run(tmp_path: Path) -> None:
    old_run = tmp_path / "old"
    current_run = tmp_path / "current"
    old_run.mkdir()
    current_run.mkdir()
    (old_run / "raw.json").write_text(json.dumps({
        "authors": {"alice": {"username": "Alice", "user_id": "42", "account_based_in": "Japan", "status": "success"}}
    }), encoding="utf-8")
    posts = [{"author_username": "Alice", "account_based_in": "", "account_lookup_status": "pending"}]
    authors: dict[str, dict[str, object]] = {}
    cache = collector.load_author_cache(current_run)
    assert collector.apply_author_cache(posts, authors, cache) == 1
    assert posts[0]["account_based_in"] == "Japan"
    assert authors["alice"]["user_id"] == "42"


def test_search_repair_queue_keeps_only_unresolved_slices() -> None:
    runs = [
        {
            "keyword": "GPT", "slice_start": "2026-01-01", "slice_end": "2026-01-01",
            "count": 0, "stop_reason": "no_network_response", "coverage_status": "needs_retry",
            "completed_at": "2026-09-29T01:00:00Z",
        },
        {
            "keyword": "GPT", "slice_start": "2026-01-02", "slice_end": "2026-01-02",
            "count": 3, "stop_reason": "no_network_response", "coverage_status": "needs_retry",
            "completed_at": "2026-09-29T01:01:00Z",
        },
        {
            "keyword": "GPT", "slice_start": "2026-01-02", "slice_end": "2026-01-02",
            "count": 19, "stop_reason": "cursor_exhausted", "coverage_status": "apparently_exhausted",
            "completed_at": "2026-09-29T01:02:00Z",
        },
        {
            "keyword": "GPT", "slice_start": "2026-01-03", "slice_end": "2026-01-03",
            "count": 42, "stop_reason": "pagination_not_triggered", "coverage_status": "needs_retry",
            "last_cursor": "saved-cursor", "completed_at": "2026-09-29T01:03:00Z",
        },
    ]

    queue = collector.search_repair_queue(runs)

    assert len(queue) == 2
    assert queue[0]["keyword"] == "GPT"
    assert queue[0]["slice_start"] == "2026-01-01"
    assert queue[0]["attempts"] == 1
    assert queue[0]["last_stop_reason"] == "no_network_response"
    assert queue[1]["slice_start"] == "2026-01-03"
    assert queue[1]["last_stop_reason"] == "pagination_not_triggered"
    assert collector.reliable_search_keys(runs) == {("GPT", "2026-01-02", "2026-01-02")}

