from __future__ import annotations

import json
import sys
import zipfile
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
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
    payload = {
        "data": {
            "user_result_by_screen_name": {
                "result": {
                    "rest_id": "147929555",
                    "about_profile": {"account_based_in": "United States"},
                }
            }
        }
    }
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
    for name in ("posts.csv", "posts_deduplicated.csv", "research_coding_template.csv", "country_summary.csv", "keyword_summary.csv", "author_summary.csv", "posted_date_summary.csv", "coverage_report.csv", "research_dataset.json", "index.html", "all_exports.zip"):
        assert (tmp_path / name).exists()
    with zipfile.ZipFile(tmp_path / "all_exports.zip") as archive:
        assert "posts_deduplicated.csv" in archive.namelist()
        assert "research_coding_template.csv" in archive.namelist()
    report = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "下载全部 ZIP" in report
    assert "账号所在地分布" in report
    assert "帖子时间分布" in report
    assert "搜索覆盖诊断" in report
    assert "滚动无新增" in report
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

