"""Sixth-account pilot: visible login, then one browser-free SearchTimeline page.

This never reads A–E profiles or writes their outputs. No password, 2FA code,
cookie, bearer token, CSRF token or request header is written to the report.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from x_account_collector import CHROME
from x_browserless_probe import ROOT, _atomic_json, _system_proxy
from x_keyword_collector import (
    _bottom_cursor,
    _search_raw_query,
    build_search_query,
    parse_search_timeline,
)


PROFILE = ROOT / "tmp" / "browserless_handoff" / "sixth-account-chrome-profile"
OUTPUT = ROOT / "outputs" / "browserless_handoff"
FORWARDED_HEADERS = {
    "accept", "accept-language", "authorization", "content-type", "referer",
    "user-agent", "x-csrf-token", "x-twitter-active-user", "x-twitter-auth-type",
    "x-twitter-client-language",
}


def _is_expected_search(url: str, body: str | None, query: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"x.com", "twitter.com"}
        and parsed.path.endswith("/SearchTimeline")
        and " ".join(_search_raw_query(url, body).split()) == " ".join(query.split())
    )


def _next_page_request(url: str, method: str, body: str | None, cursor: str) -> tuple[str, str | None]:
    if not cursor:
        raise ValueError("第一页没有下一页游标")
    if method == "GET":
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True)
        values = query.get("variables")
        if not values:
            raise ValueError("搜索请求缺少 variables")
        variables = json.loads(values[0])
        if not isinstance(variables, dict):
            raise ValueError("variables 不是 JSON 对象")
        variables["cursor"] = cursor
        query["variables"] = [json.dumps(variables, ensure_ascii=False, separators=(",", ":"))]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)), None
    if method == "POST" and body:
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("variables"), dict):
            raise ValueError("POST 搜索请求缺少 variables")
        payload["variables"]["cursor"] = cursor
        return url, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raise ValueError("不支持的搜索请求方法")


def _http_cookies(browser_cookies: list[dict[str, object]]) -> httpx.Cookies:
    cookies = httpx.Cookies()
    for item in browser_cookies:
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if isinstance(name, str) and isinstance(value, str) and isinstance(domain, str):
            cookies.set(name, value, domain=domain, path=str(item.get("path") or "/"))
    return cookies


def _safe_header_summary(headers: httpx.Headers) -> dict[str, str]:
    return {key: headers.get(key, "") for key in (
        "retry-after", "x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset"
    )}


def run_handoff(keyword: str, day: date, delay_seconds: float = 3.0) -> dict[str, object]:
    if delay_seconds < 2:
        raise ValueError("两页之间至少等待 2 秒")
    if not CHROME.is_file():
        raise RuntimeError("找不到本机 Chrome，无法让用户正常登录")
    query = build_search_query(keyword, day, day)
    search_url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=live"
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(PROFILE), executable_path=str(CHROME), headless=False,
            locale="en-US", viewport={"width": 1400, "height": 1000},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=90_000)
            print("请在刚打开的浏览器里自行完成第六账号的正常登录和 2FA。完成后回到此终端按 Enter；不要把密码发给程序。", flush=True)
            input()
            try:
                with page.expect_response(
                    lambda response: _is_expected_search(response.url, response.request.post_data, query),
                    timeout=90_000,
                ) as pending:
                    page.goto(search_url, wait_until="domcontentloaded", timeout=90_000)
                first = pending.value
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("没有观察到当前查询的 SearchTimeline 响应；不尝试直接请求") from exc
            if first.status != 200:
                return {"phase": "browser_first_page", "status": first.status,
                        "reason": "rate_limited" if first.status == 429 else "browser_search_failed"}
            payload = first.json()
            if not isinstance(payload, dict):
                raise RuntimeError("浏览器第一页不是 JSON 对象")
            first_rows, cursor = parse_search_timeline(payload, keyword, 1)
            request = first.request
            request_headers = request.all_headers()
            headers = {key: value for key, value in request_headers.items()
                       if key.lower() in FORWARDED_HEADERS}
            original_url, method, body = first.url, request.method, request.post_data
            browser_cookies = context.cookies("https://x.com")
            first_count = len(first_rows)
        finally:
            context.close()

    # No Chromium process from this pilot remains when this request is issued.
    if not cursor:
        return {"phase": "browser_first_page", "status": 200,
                "first_page_posts": first_count, "reason": "no_cursor"}
    url, next_body = _next_page_request(original_url, method, body, cursor)
    if not _is_expected_search(url, next_body, query):
        raise RuntimeError("翻页请求与当前关键词和日期不匹配；拒绝发送")
    time.sleep(delay_seconds)
    started = time.monotonic()
    with httpx.Client(proxy=_system_proxy(), cookies=_http_cookies(browser_cookies),
                      timeout=30, follow_redirects=False) as client:
        response = client.request(method, url, headers=headers, content=next_body)
    result: dict[str, object] = {
        "phase": "browser_closed_http_next_page", "status": response.status_code,
        "first_page_posts": first_count, "elapsed_http_seconds": round(time.monotonic() - started, 2),
        "rate_headers": _safe_header_summary(response.headers),
        "coverage_status": "pilot_only_not_complete",
    }
    if response.status_code == 429:
        result["reason"] = "rate_limited_no_retry"
    elif response.status_code in {401, 403}:
        result["reason"] = "authentication_or_permission_no_retry"
    elif response.status_code != 200:
        result["reason"] = "http_error_no_retry"
    else:
        try:
            next_payload = response.json()
        except ValueError:
            result["reason"] = "not_json"
        else:
            if isinstance(next_payload, dict):
                next_rows, next_cursor = parse_search_timeline(next_payload, keyword, 1)
                result["second_page_posts"] = len(next_rows)
                result["overlap_posts"] = len({row["post_id"] for row in first_rows} &
                                              {row["post_id"] for row in next_rows})
                result["next_cursor_present"] = bool(next_cursor)
                result["posts"] = first_rows + next_rows
                result["reason"] = "http_handoff_success"
            else:
                result["reason"] = "unexpected_json"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第六账号：可见登录后关闭浏览器，用 HTTP 验证下一页")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--delay-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    try:
        result = run_handoff(args.keyword, date.fromisoformat(args.date), args.delay_seconds)
    except (RuntimeError, ValueError, httpx.RequestError, PlaywrightError) as exc:
        # Do not print exception text: HTTP exceptions can contain a request URL.
        print(f"试验未完成：{type(exc).__name__}。没有修改现有采集批次。", file=sys.stderr)
        return 1
    output = OUTPUT / f"{args.date}_{int(time.time())}.json"
    _atomic_json(output, result)
    print(json.dumps({"status": result.get("status"), "reason": result.get("reason"),
                      "first_page_posts": result.get("first_page_posts"),
                      "second_page_posts": result.get("second_page_posts"),
                      "output": str(output)}, ensure_ascii=False))
    return 0 if result.get("reason") == "http_handoff_success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
