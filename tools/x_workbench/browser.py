"""browser components; no task starts on import."""
from __future__ import annotations
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from .metrics import timed
from .request_identity import _search_response_matches, _response_headers, _search_raw_query, _search_request_cursor, _search_url_with_cursor


class _BufferedSearchResponse:
    """Snapshot a browser-created response before Chrome evicts its body.

    No requests are issued. Preserve headers and errors for existing protection
    and authoritative reset handling instead of converting failures to empties.
    """
    def __init__(self, response):
        self.original = response
        self.url, self.status, self.request = response.url, response.status, response.request
        self._headers = _response_headers(response)
        self._payload = None
        self._error = None
        self._failure_body = None
        if self.status == 200:
            try:
                self._payload = response.json()
            except Exception as exc:
                self._error = exc
        elif self.status == 429:
            self._failure_body = {'body_not_read_for_rate_limit':True}
        else:
            try:
                self._failure_body = response.json()
            except Exception:
                try:
                    self._failure_body = {'text':response.text()[:262144]}
                except Exception as exc:
                    self._failure_body = {'body_unavailable':str(exc)}

    def header_value(self, name):
        return self._headers.get(name.lower(), "")

    def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


def _take_buffered_response(response, buffered_responses):
    if buffered_responses is not None:
        for index, cached in enumerate(buffered_responses):
            if getattr(cached, 'original', None) is response:
                return buffered_responses.pop(index)
    return response


def _scroll_last_tweet_if_attached(tweets):
    """Virtualized tweets can detach between lookup and scrolling; retain the UI trigger."""
    try:
        tweets.last.scroll_into_view_if_needed(timeout=5_000)
        return tweets.last.bounding_box()
    except Exception as exc:
        if "Element is not attached to the DOM" not in str(exc):
            raise
        print("  [分页诊断] 时间线元素已被虚拟化移除，继续原滚动触发；不算查询结束。", flush=True)
        return None


@timed("request_wait")
def _next_browser_signed_search_response(page, query: str, attempts: int = 3, timeout_ms: int = 15_000, expected_cursor: str | None = None, buffered_responses=None):
    """Let X create a fresh transaction id, retrying the physical pagination trigger when the UI stalls."""
    for attempt in range(1, attempts + 1):
        if buffered_responses is not None:
            # The UI may prefetch while we parse/save the previous page. Do not
            # wait for a second physical trigger of an already-issued request.
            page.wait_for_timeout(1)
            for index, response in enumerate(buffered_responses):
                if _search_response_matches(response, query, expected_cursor):
                    buffered_responses.pop(index)
                    print("  [分页诊断] 使用已捕获且查询/游标匹配的浏览器预加载响应。", flush=True)
                    return response, attempt
        try:
            with page.expect_response(
                lambda response: _search_response_matches(response, query, expected_cursor),
                timeout=timeout_ms,
            ) as pending:
                # X virtualizes its timeline, so use increasingly strong UI gestures.
                # The request remains created by X itself (including its one-use
                # transaction id); we only retrigger the visible pagination sentinel.
                if attempt == 1:
                    # Position the pointer inside the actual timeline rather
                    # than scrolling whichever pane currently owns the mouse.
                    tweets = page.locator("article[data-testid='tweet']")
                    if tweets.count():
                        box = _scroll_last_tweet_if_attached(tweets)
                        if box:
                            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 40))
                    page.mouse.wheel(0, 3200)
                    page.keyboard.press("End")
                elif attempt == 2:
                    page.mouse.wheel(0, -900)
                    page.wait_for_timeout(500)
                    page.mouse.wheel(0, 5200)
                    page.keyboard.press("End")
                else:
                    tweets = page.locator("article[data-testid='tweet']")
                    if tweets.count():
                        _scroll_last_tweet_if_attached(tweets)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.keyboard.press("End")
            return _take_buffered_response(pending.value, buffered_responses), attempt
        except PlaywrightTimeoutError:
            print(f"  [分页诊断] 触发 {attempt}/{attempts} 超时；未收到当前查询且请求游标匹配的响应，不算游标耗尽。", flush=True)
            if attempt < attempts:
                page.wait_for_timeout(700 + attempt * 500)
    return None, attempts


@timed("request_wait")
def _fresh_browser_cursor_response(page, query, cursor, buffered_responses=None):
    """Verify a candidate empty tail with one fresh browser-signed request."""
    redirected = []
    def redirect(route):
        request = route.request
        if not redirected and request.method == 'GET' and _search_raw_query(request.url, request.post_data) == query and _search_request_cursor(request.url, request.post_data) == '':
            redirected.append(True)
            route.continue_(url=_search_url_with_cursor(request.url, cursor))
        else:
            route.continue_()
    page.route('**/*SearchTimeline*', redirect)
    try:
        with page.expect_response(lambda r: _search_response_matches(r, query, cursor), timeout=60_000) as pending:
            page.reload(wait_until='domcontentloaded', timeout=60_000)
        return _take_buffered_response(pending.value, buffered_responses)
    finally:
        page.unroute('**/*SearchTimeline*', redirect)
