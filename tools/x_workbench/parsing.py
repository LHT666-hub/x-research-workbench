"""parsing components; no task starts on import."""
from __future__ import annotations
import email.utils
from datetime import timezone
from .clock import now_iso
from .metrics import timed


class SearchRateLimitError(RuntimeError):
    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("SearchTimeline HTTP 429")
        self.retry_after = retry_after


def _nested_dict(value: object, *keys: str) -> dict[str, object]:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _unwrap_tweet_result(result: object) -> dict[str, object]:
    current = result
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        if isinstance(current.get("tweet"), dict) and not isinstance(current.get("legacy"), dict):
            current = current["tweet"]
            continue
        if isinstance(current.get("result"), dict) and not isinstance(current.get("legacy"), dict):
            current = current["result"]
            continue
        break
    return current if isinstance(current, dict) else {}


def _tweet_results(payload: object):
    """Yield only primary timeline tweet results, not nested quote/retweet payloads."""
    if isinstance(payload, dict):
        tweet_results = payload.get("tweet_results")
        if isinstance(tweet_results, dict) and isinstance(tweet_results.get("result"), dict):
            yield _unwrap_tweet_result(tweet_results["result"])
            return
        for value in payload.values():
            yield from _tweet_results(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _tweet_results(value)


def _bottom_cursor(payload: object) -> str:
    candidates: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            entry_id = str(value.get("entryId", value.get("entry_id", "")) or "").casefold()
            cursor_type = str(value.get("cursorType", value.get("cursor_type", "")) or "").casefold()
            content = value.get("content") if isinstance(value.get("content"), dict) else value
            cursor = ""
            if isinstance(content, dict):
                cursor = str(content.get("value", "") or "")
                cursor_type = str(content.get("cursorType", content.get("cursor_type", cursor_type)) or cursor_type).casefold()
            if cursor and ("cursor-bottom" in entry_id or cursor_type == "bottom"):
                candidates.append(cursor)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return candidates[-1] if candidates else ""


def _twitter_time_to_iso(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return email.utils.parsedate_to_datetime(text).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return text


def validate_search_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise RuntimeError("SearchTimeline 响应不是 JSON 对象")
    errors = payload.get("errors")
    if errors:
        codes = [str(error.get("code", "unknown")) for error in errors if isinstance(error, dict)] if isinstance(errors, list) else ["unknown"]
        if "88" in codes:
            raise SearchRateLimitError(None)
        raise RuntimeError("SearchTimeline 返回接口错误，code=" + ",".join(codes))


@timed("parse")
def parse_search_timeline(payload: object, keyword: str, keyword_index: int) -> tuple[list[dict[str, object]], str]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    collected_at = now_iso()
    for raw_result in _tweet_results(payload):
        result = _unwrap_tweet_result(raw_result)
        legacy = result.get("legacy") if isinstance(result.get("legacy"), dict) else {}
        post_id = str(result.get("rest_id") or legacy.get("id_str") or "")
        if not post_id or post_id in seen:
            continue
        user_result = _nested_dict(result, "core", "user_results", "result")
        if not user_result:
            user_result = _nested_dict(result, "author_results", "result")
        user_result = _unwrap_tweet_result(user_result)
        user_legacy = user_result.get("legacy") if isinstance(user_result.get("legacy"), dict) else {}
        user_core = user_result.get("core") if isinstance(user_result.get("core"), dict) else {}
        user_location = user_result.get("location") if isinstance(user_result.get("location"), dict) else {}
        profile_location = str(user_location.get("location") or user_legacy.get("location") or "")
        username = str(user_core.get("screen_name") or user_legacy.get("screen_name") or "")
        if not username:
            continue
        note = _nested_dict(result, "note_tweet", "note_tweet_results", "result")
        text = str(note.get("text") or legacy.get("full_text") or legacy.get("text") or "").strip()
        if not text:
            continue
        seen.add(post_id)
        rows.append({
            "keyword": keyword,
            "keyword_index": keyword_index,
            "rank": 0,
            "post_id": post_id,
            "post_url": f"https://x.com/{username}/status/{post_id}",
            "post_text": text,
            "posted_at": _twitter_time_to_iso(legacy.get("created_at")),
            "time_label": "",
            "author_username": username,
            "author_display_name": str(user_core.get("name") or user_legacy.get("name") or ""),
            "author_user_id": str(user_result.get("rest_id") or user_legacy.get("id_str") or ""),
            "profile_location": profile_location,
            "profile_location_status": "success" if profile_location else "pending",
            "profile_location_error": "",
            "profile_location_collected_at": collected_at if profile_location else "",
            "account_based_in": "",
            "source": "search_timeline_graphql",
            "account_lookup_status": "pending",
            "collected_at": collected_at,
        })
    return rows, _bottom_cursor(payload)
