"""Experimental direct-HTTP boundary; not wired to the production CLI.

No account lookup, login, browser-profile access, signing implementation, retries
or rotation. An explicit caller supplies an isolated client and fresh legitimate
signed headers. Offline mocks test the boundary, not X session compatibility.
"""
from dataclasses import dataclass, field
import json
from urllib.parse import parse_qs, urlsplit

from .metrics import timed
from .request_identity import _search_raw_query, _search_request_cursor, _search_url_with_cursor


class HttpTransportError(RuntimeError):
    pass


@dataclass(repr=False)
class HttpSearchResponse:
    status: int
    url: str
    request: object = field(repr=False)
    _response: object = field(repr=False)

    def __repr__(self):
        return f"HttpSearchResponse(status={self.status})"

    def json(self):
        # Do not turn unreadable JSON into an empty successful response.
        return self._response.json()

    def header_value(self, name):
        if name.lower() not in {"retry-after", "x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset"}:
            return ""
        return self._response.headers.get(name.lower(), "")


@dataclass
class _RequestIdentity:
    post_data: object = None
    method: str = "GET"

    def all_headers(self):
        # Common pipeline needs audit-safe request metadata, not credentials.
        return {}


class DirectHttpSearchTransport:
    """Persistent caller-owned HTTP client; fresh signing required per request."""
    def __init__(self, template_url, query, client, fresh_headers, *, enable_network=False):
        self._validate_url(template_url)
        if _search_raw_query(template_url) != query:
            raise ValueError("HTTP template query mismatch")
        variables = json.loads(parse_qs(urlsplit(template_url).query)["variables"][0])
        if variables.get("product") != "Latest":
            raise ValueError("Experimental HTTP comparison requires Latest")
        self.template_url, self.query = template_url, query
        self.client, self.fresh_headers = client, fresh_headers
        self.enable_network = enable_network

    @staticmethod
    def _validate_url(url):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"x.com", "twitter.com"}
                or parts.username or parts.password or parts.port not in {None, 443}
                or not parts.path.startswith("/i/api/graphql/") or not parts.path.endswith("/SearchTimeline")
                or parts.fragment):
            raise ValueError("Only the explicit HTTPS SearchTimeline endpoint is allowed")

    @timed("request_wait")
    def fetch(self, cursor):
        if not self.enable_network:
            raise HttpTransportError("Experimental HTTP requests are disabled")
        url = _search_url_with_cursor(self.template_url, cursor)
        # Never reuse a recorded transaction-id header from an old browser page.
        headers = self.fresh_headers("GET", urlsplit(url).path)
        if not isinstance(headers, dict) or not any(key.lower() == "x-client-transaction-id" and value for key, value in headers.items()):
            raise HttpTransportError("Fresh request signing provider required")
        try:
            response = self.client.request("GET", url, headers=headers, timeout=60, follow_redirects=False)
        except Exception as exc:
            # Deliberately omit messages/repr that may contain credential headers.
            raise HttpTransportError("HTTP client failed: " + type(exc).__name__) from None
        self._validate_url(str(response.url))
        if (_search_raw_query(str(response.url)) != self.query
                or _search_request_cursor(str(response.url)) != cursor):
            raise HttpTransportError("Returned HTTP query/cursor mismatch")
        # 401/403/404/429 bodies remain visible to the common protection policy.
        # No silent retry, account rotation or conversion to an empty success.
        return HttpSearchResponse(response.status_code, str(response.url), _RequestIdentity(), response)
