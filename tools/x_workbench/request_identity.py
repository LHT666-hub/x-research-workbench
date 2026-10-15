"""request_identity components; no task starts on import."""
from __future__ import annotations
import json
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def _search_url_with_cursor(url: str, cursor: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    values = query.get("variables")
    if not values:
        raise RuntimeError("SearchTimeline 请求中没有 variables")
    variables = json.loads(values[0])
    if not isinstance(variables, dict):
        raise RuntimeError("SearchTimeline variables 格式无效")
    if cursor:
        variables["cursor"] = cursor
    else:
        variables.pop("cursor", None)
    query["variables"] = [json.dumps(variables, separators=(",", ":"), ensure_ascii=False)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def _search_raw_query(url: str, body: str | None = None) -> str:
    """Extract rawQuery so a delayed response from an older date slice cannot be mistaken for the new one."""
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("variables", [])
        variables = json.loads(values[0]) if values else {}
        if isinstance(variables, dict) and variables.get("rawQuery") is not None:
            return str(variables["rawQuery"])
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    if body:
        try:
            parsed = json.loads(body)
            variables = parsed.get("variables", parsed) if isinstance(parsed, dict) else {}
            if isinstance(variables, dict) and variables.get("rawQuery") is not None:
                return str(variables["rawQuery"])
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return ""


def _search_request_cursor(url: str, body: str | None = None) -> str:
    """Read the request cursor, never replay requests or expose signed headers."""
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("variables", [])
    for value in [*(values[:1]), body]:
        if not value:
            continue
        try:
            parsed = json.loads(value)
            variables = parsed.get("variables", parsed) if isinstance(parsed, dict) else {}
            if isinstance(variables, str):
                variables = json.loads(variables)
            if isinstance(variables, dict):
                return str(variables.get("cursor") or "")
        except (ValueError, TypeError):
            continue
    return ""


def _search_response_matches(response, expected_query: str, expected_cursor: str | None = None) -> bool:
    if "SearchTimeline" not in response.url:
        return False
    actual = _search_raw_query(response.url, response.request.post_data)
    if " ".join(actual.split()) != " ".join(expected_query.split()):
        return False
    # A matching-query HTTP error must reach the existing reset/guard handling;
    # it must not be misreported as a pagination timeout.
    if response.status != 200:
        return True
    return expected_cursor is None or _search_request_cursor(response.url, response.request.post_data) == expected_cursor


def _response_headers(response) -> dict[str, str]:
    return {
        name: response.header_value(name) or ""
        for name in ("retry-after", "x-rate-limit-limit", "x-rate-limit-reset", "x-rate-limit-remaining")
    }


def _search_template(response) -> dict[str, object]:
    request = response.request
    forwarded = {
        "authorization", "content-type", "x-csrf-token", "x-twitter-active-user",
        "x-twitter-auth-type", "x-twitter-client-language",
    }
    return {
        "url": response.url,
        "method": request.method,
        "headers": {key: value for key, value in request.all_headers().items() if key.lower() in forwarded},
        "body": request.post_data,
    }


def _graphql_operation(url: str) -> tuple[str, str]:
    """Return audit-safe endpoint metadata without cookies or authorization."""
    path = urlsplit(url).path
    operation = path.rstrip("/").split("/")[-1] if path else ""
    return operation, path
