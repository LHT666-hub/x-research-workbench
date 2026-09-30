from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, quote, urlsplit

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import x_browserless_handoff as handoff  # noqa: E402


QUERY = "GPT since:2026-01-01 until:2026-01-01"
URL = "https://x.com/i/api/graphql/observed/SearchTimeline?variables=" + quote(
    json.dumps({"rawQuery": QUERY}, separators=(",", ":"))
)


def test_only_accepts_matching_actual_search_query():
    assert handoff._is_expected_search(URL, None, QUERY)
    assert not handoff._is_expected_search(URL, None, "Claude since:2026-01-01 until:2026-01-01")
    assert not handoff._is_expected_search(URL.replace("x.com", "example.com"), None, QUERY)


def test_get_pagination_uses_observed_url_and_sets_cursor():
    url, body = handoff._next_page_request(URL, "GET", None, "next-cursor")
    assert body is None
    variables = json.loads(parse_qs(urlsplit(url).query)["variables"][0])
    assert variables == {"rawQuery": QUERY, "cursor": "next-cursor"}
    assert handoff._is_expected_search(url, body, QUERY)


def test_post_pagination_uses_observed_body():
    original = json.dumps({"variables": {"rawQuery": QUERY}, "features": {"x": True}})
    url, body = handoff._next_page_request(URL.split("?")[0], "POST", original, "next-cursor")
    assert url == URL.split("?")[0]
    assert json.loads(body)["variables"]["cursor"] == "next-cursor"
    assert handoff._is_expected_search(url, body, QUERY)


def test_pagination_requires_cursor_and_valid_variables():
    with pytest.raises(ValueError, match="游标"):
        handoff._next_page_request(URL, "GET", None, "")
    with pytest.raises(ValueError, match="variables"):
        handoff._next_page_request("https://x.com/i/api/graphql/observed/SearchTimeline", "GET", None, "x")


def test_cookie_values_are_only_in_memory_and_not_in_header_summary():
    cookies = handoff._http_cookies([{"name": "session", "value": "secret", "domain": ".x.com", "path": "/"}])
    assert "secret" in str(cookies)
    import httpx
    headers = httpx.Headers({"retry-after": "60", "authorization": "secret", "set-cookie": "secret"})
    safe = handoff._safe_header_summary(headers)
    assert safe["retry-after"] == "60"
    assert "secret" not in str(safe)
