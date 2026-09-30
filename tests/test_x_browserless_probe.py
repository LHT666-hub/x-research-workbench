from __future__ import annotations

import asyncio
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import x_browserless_probe as probe  # noqa: E402


def tweet(post_id: str, hour: int = 0, location: str = "Tokyo") -> SimpleNamespace:
    user = SimpleNamespace(screen_name="alice", id="42", location=location)
    return SimpleNamespace(id=post_id, user=user, text="short", full_text="Original text",
                           created_at_datetime=datetime(2025, 12, 31, hour, tzinfo=timezone.utc))


class FakeResult(list):
    def __init__(self, items, next_page=None):
        super().__init__(items)
        self.next_page = next_page

    async def next(self):
        return self.next_page


class FakeClient:
    def __init__(self, page):
        self.page = page
        self.calls = []

    async def search_tweet(self, query, product, count):
        self.calls.append((query, product, count))
        return self.page


def test_normalize_uses_profile_location_not_account_based_in():
    row = probe.normalize_tweet(tweet("123", 16))
    assert row == {
        "post_id": "123", "post_url": "https://x.com/alice/status/123",
        "post_text": "Original text", "posted_at": "2025-12-31T16:00:00Z",
        "author_username": "alice", "author_user_id": "42", "profile_location": "Tokyo",
        "account_based_in": None, "source": "twikit_search_timeline_probe",
    }


def test_probe_deduplicates_and_filters_local_day():
    first = FakeResult([tweet("1", 16), tweet("2", 15)],
                       FakeResult([tweet("1", 16), tweet("3", 17)]))
    client = FakeClient(first)
    delays = []

    async def no_sleep(seconds):
        delays.append(seconds)

    result = asyncio.run(probe.probe_pages(client, "GPT since:2026-01-01 until:2026-01-01",
                                           date(2026, 1, 1), 2, 3, no_sleep))
    assert client.calls == [("GPT since:2026-01-01 until:2026-01-01", "Latest", 20)]
    assert [post["post_id"] for post in result["posts"]] == ["1", "3"]
    assert result["pages"] == [{"page": 1, "returned": 2, "added_in_date": 1},
                               {"page": 2, "returned": 2, "added_in_date": 1}]
    assert result["coverage_status"] == "pilot_only_not_complete"
    assert delays == [3]


def test_probe_stops_on_429_without_retry():
    class RateLimitError(Exception):
        status_code = 429

    class LimitedClient:
        def __init__(self):
            self.calls = 0

        async def search_tweet(self, *_args, **_kwargs):
            self.calls += 1
            raise RateLimitError("secret request information")

    client = LimitedClient()
    result = asyncio.run(probe.probe_pages(client, "GPT", date(2026, 1, 1)))
    assert client.calls == 1
    assert result["stop_reason"] == "rate_limited"
    assert result["posts"] == []
    assert "secret request information" not in json.dumps(result)


def test_rejects_cookie_file_inside_tracked_repository():
    with pytest.raises(ValueError, match="tmp/"):
        probe._safe_cookie_path(probe.ROOT / "cookies.json")
    assert probe._safe_cookie_path(probe.DEFAULT_COOKIES) == probe.DEFAULT_COOKIES.resolve()


def test_read_only_overlap_comparison(tmp_path):
    old = {"posts": [{"post_id": "1", "posted_at": "2025-12-31T16:00:00Z"},
                     {"post_id": "2", "posted_at": "2025-12-31T15:59:59Z"}]}
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(old), encoding="utf-8")
    result = probe.compare_with_raw({"date": "2026-01-01", "posts": [{"post_id": "1"},
                                                              {"post_id": "3"}]}, raw)
    assert result["historical_day_count"] == 1
    assert result["overlap_count"] == 1
    assert result["probe_overlap_fraction"] == 0.5


def test_search_endpoint_must_come_from_observed_get_csv(tmp_path):
    coverage = tmp_path / "coverage.csv"
    coverage.write_text(
        "search_engine,request_method,endpoint_path\n"
        "graphql,GET,/i/api/graphql/observed_hash_123/SearchTimeline\n",
        encoding="utf-8",
    )
    assert probe.observed_search_endpoint(coverage) == (
        "https://x.com/i/api/graphql/observed_hash_123/SearchTimeline"
    )
    coverage.write_text(
        "search_engine,request_method,endpoint_path\n"
        "graphql,GET,https://attacker.example/x\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="拒绝猜测"):
        probe.observed_search_endpoint(coverage)


def test_visible_login_prompt_does_not_call_network_for_empty_password(tmp_path, monkeypatch, capsys):
    answers = iter(["testaccount", "optional@example.com", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(probe, "_twikit_client", lambda: pytest.fail("must not contact X"))
    args = argparse.Namespace(cookies=tmp_path / "cookies.json", username=None)
    assert asyncio.run(probe._login(args)) == 2
    assert "会显示在当前终端" in capsys.readouterr().out
    assert not args.cookies.exists()
