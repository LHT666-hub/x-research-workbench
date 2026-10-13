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


def test_virtualized_tweet_detachment_keeps_pagination_trigger():
    class Tweet:
        @property
        def last(self):
            return self
        def scroll_into_view_if_needed(self, **kwargs):
            raise RuntimeError('Element is not attached to the DOM')
    assert collector._scroll_last_tweet_if_attached(Tweet()) is None


def test_other_scroll_errors_are_not_hidden():
    class Tweet:
        @property
        def last(self):
            return self
        def scroll_into_view_if_needed(self, **kwargs):
            raise RuntimeError('Browser closed')
    with pytest.raises(RuntimeError, match='Browser closed'):
        collector._scroll_last_tweet_if_attached(Tweet())


@pytest.mark.parametrize('status', [401, 403, 404, 429, 500])
def test_search_http_errors_are_visible_not_pagination_timeout(status):
    from types import SimpleNamespace
    from urllib.parse import urlencode
    query='GPT since_time:1 until_time:2'
    response=SimpleNamespace(status=status, request=SimpleNamespace(post_data=None),
        url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':'old'})}))
    assert collector._search_response_matches(response, query, 'next')
    assert not collector._search_response_matches(response, 'another query', 'next')


def test_search_page_requires_query_and_cursor():
    from types import SimpleNamespace
    from urllib.parse import urlencode
    query='GPT since_time:1 until_time:2'
    response=SimpleNamespace(status=200,request=SimpleNamespace(post_data=None),
        url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':'next'})}))
    assert collector._search_response_matches(response,query,'next')
    assert not collector._search_response_matches(response,query,'old')
    assert not collector._search_response_matches(response,query,'')
    assert collector._search_request_cursor('https://x.com/i/api/graphql/hash/SearchTimeline',json.dumps({'variables':{'rawQuery':query,'cursor':'body-next'}}))=='body-next'


def test_prefetched_next_page_is_used_without_another_scroll():
    from types import SimpleNamespace
    from urllib.parse import urlencode
    query='GPT since_time:1 until_time:2'
    def response(cursor):
        return SimpleNamespace(status=200,request=SimpleNamespace(post_data=None),
            url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':cursor})}))
    old=response('old'); next_page=response('next'); buffered=[old,next_page]
    class Page:
        def wait_for_timeout(self, ms): pass
        def expect_response(self,*a,**k): raise AssertionError('must consume prefetch, not retrigger')
    captured,attempt=collector._next_browser_signed_search_response(Page(),query,expected_cursor='next',buffered_responses=buffered)
    assert captured is next_page and attempt==1 and buffered==[old]


def test_response_listener_removed_on_failure(monkeypatch,tmp_path):
    events=[]
    class Page:
        def on(self,event,callback): events.append(('on',event,callback))
        def remove_listener(self,event,callback): events.append(('off',event,callback))
    def fail(*a,**k): raise RuntimeError('test failure')
    monkeypatch.setattr(collector,'_collect_search_graphql_impl',fail)
    with pytest.raises(RuntimeError,match='test failure'):
        collector.collect_search_graphql(Page(),'GPT',1,0,4,tmp_path)
    assert events[0][0]=='on' and events[1][0]=='off' and events[0][2] is events[1][2]


def test_page_bound_does_not_fetch_unprocessed_extra_page(monkeypatch,tmp_path):
    from types import SimpleNamespace
    response=SimpleNamespace(status=200,url='https://x.com/i/api/graphql/hash/SearchTimeline',
        request=SimpleNamespace(method='GET',post_data=None,all_headers=lambda:{}),
        header_value=lambda name:None,json=lambda:{'data':{}})
    class Pending:
        value=response
        def __enter__(self): return self
        def __exit__(self,*a): pass
    class Page:
        def expect_response(self,*a,**k): return Pending()
        def goto(self,*a,**k): pass
    monkeypatch.setattr(collector,'parse_search_timeline',lambda *a:([{'post_id':'1','posted_at':'2026-09-01T01:00:00Z'}],'next'))
    def extra(*a,**k): raise AssertionError('must not fetch beyond the page bound')
    monkeypatch.setattr(collector,'_next_browser_signed_search_response',extra)
    rows,coverage=collector.collect_search_graphql(Page(),'GPT',1,0,1,tmp_path,date(2026,9,1),date(2026,9,1))
    assert len(rows)==1 and coverage['stop_reason']=='max_pages' and coverage['json_pages_used']==1


def test_buffered_body_survives_browser_body_eviction():
    from types import SimpleNamespace
    calls=[]
    def body():
        calls.append(1)
        if len(calls)>1: raise RuntimeError('No data found for resource')
        return {'data':{'persisted':True}}
    response=SimpleNamespace(url='https://x.com/SearchTimeline',status=200,request=None,
        json=body,header_value=lambda name:'12345' if name=='x-rate-limit-reset' else '')
    cached=collector._BufferedSearchResponse(response)
    assert cached.json()=={'data':{'persisted':True}}
    assert cached.json()=={'data':{'persisted':True}} and len(calls)==1
    assert cached.header_value('x-rate-limit-reset')=='12345'
    buffer=[cached]
    assert collector._take_buffered_response(response,buffer) is cached and buffer==[]


def test_buffered_body_failure_keeps_reset_and_is_not_empty(monkeypatch):
    from types import SimpleNamespace
    def broken(): raise RuntimeError('No data found for resource')
    response=SimpleNamespace(url='https://x.com/SearchTimeline',status=200,request=None,
        json=broken,header_value=lambda name:'1300' if name=='x-rate-limit-reset' else '')
    monkeypatch.setattr(collector.time,'time',lambda:1000)
    cached=collector._BufferedSearchResponse(response)
    with pytest.raises(RuntimeError,match='No data found') as exc:
        collector.checked_search_response(cached)
    assert exc.value.search_retry_after==300


def test_buffered_rate_limit_does_not_read_body(monkeypatch):
    from types import SimpleNamespace
    def body(): raise AssertionError('must not fetch a 429 body')
    response=SimpleNamespace(url='https://x.com/SearchTimeline',status=429,request=None,
        json=body,header_value=lambda name:'60' if name=='retry-after' else '')
    cached=collector._BufferedSearchResponse(response)
    with pytest.raises(collector.SearchRateLimitError) as exc:
        collector.checked_search_response(cached)
    assert exc.value.retry_after==60


def test_tab_txt_preserves_all_rows_and_single_line_text(tmp_path):
    import x_export_column_txt as exporter
    p={'keyword':'GPT','post_text':'GPT\nline\tcolumn\\literal"quote','post_id':'123','posted_at':'2026-01-01T01:00:00Z'}
    dest=tmp_path/'export.txt'
    exporter.export_rows([p,{**p,'post_text':'unrelated'}],'A',dest)
    lines=dest.read_text(encoding='utf-8-sig').splitlines()
    assert len(lines)==3
    assert all(len(line.split('\t'))==12 for line in lines)
    assert 'GPT\\nline\\tcolumn\\\\literal"quote' in lines[1]
    assert lines[2].split('\t')[8]=='False'


def test_settle_abandoned_checkpoint_preserves_history_and_does_not_repeat():
    original = dict(keyword='D', slice_start='2026-07-25', slice_end='2026-07-25',
                    count=500, stop_reason='in_progress', checkpoint_at='old')
    runs = [original.copy()]
    assert collector.settle_saved_in_progress(runs) == 1
    assert runs[0] == original
    assert runs[1]['stop_reason'] == 'interrupted_with_saved_sample'
    assert not collector.search_repair_queue(runs, 'coverage_first')
    assert not collector.reliable_search_keys(runs)
    assert collector.settle_saved_in_progress(runs) == 0


def test_settlement_does_not_change_empty_or_finished_records():
    runs = [dict(keyword='D', count=0, stop_reason='in_progress')]
    assert collector.settle_saved_in_progress(runs) == 0


def test_coverage_first_only_repairs_failed_empty_days() -> None:
    runs = [
        {"keyword": "GPT", "slice_start": "2026-01-01", "count": 20, "stop_reason": "pagination_not_triggered", "coverage_status": "needs_retry"},
        {"keyword": "GPT", "slice_start": "2026-01-02", "count": 0, "stop_reason": "no_network_response", "coverage_status": "needs_retry"},
        {"keyword": "GPT", "slice_start": "2026-01-03", "count": 0, "stop_reason": "cursor_exhausted"},
    ]
    assert len(collector.search_repair_queue(runs)) == 3
    assert [row["slice_start"] for row in collector.search_repair_queue(runs, "coverage_first")] == ["2026-01-02"]
    assert len(collector.reliable_search_keys(runs)) == 0
    assert len(collector.accepted_search_keys(runs)) == 2


def test_zero_growth_is_not_verified_empty_and_partial_days_stay_accepted():
    runs = [dict(keyword='deepseek', slice_start='2026-09-10', slice_end='2026-09-10', count=0, stop_reason='no_new_results'),
            dict(keyword='deepseek', slice_start='2026-09-11', slice_end='2026-09-11', count=20, stop_reason='pagination_not_triggered'),
            dict(keyword='deepseek', slice_start='2026-09-12', slice_end='2026-09-12', count=0, stop_reason='cursor_exhausted')]
    assert [r['slice_start'] for r in collector.search_repair_queue(runs, 'coverage_first')] == ['2026-09-10']
    assert ('deepseek', '2026-09-11', '2026-09-11') in collector.accepted_search_keys(runs)
    assert ('deepseek', '2026-09-12', '2026-09-12') not in collector.reliable_search_keys(runs)
    assert ('deepseek', '2026-09-12', '2026-09-12') in collector.accepted_search_keys(runs)


def test_out_of_range_response_is_retryable_not_empty(monkeypatch, tmp_path):
    class Request:
        method = 'GET'
        post_data = None
        def all_headers(self): return {}
    class Response:
        status = 200
        request = Request()
        url = 'https://x.com/i/api/graphql/hash/SearchTimeline'
        def json(self): return {'data': {}}
        def all_headers(self): return {}
        def header_value(self, name): return None
    class Pending:
        value = Response()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Page:
        def expect_response(self, *args, **kwargs): return Pending()
        def goto(self, *args, **kwargs): pass
    monkeypatch.setattr(collector, 'parse_search_timeline', lambda *args: ([{'post_id':'1','posted_at':'2026-09-11T05:58:53Z'}], ''))
    rows, cov = collector.collect_search_graphql(Page(), 'deepseek', 1, 0, 8, tmp_path, date(2026,9,10), date(2026,9,10))
    assert not rows and cov['stop_reason'] == 'date_filter_mismatch'
    assert cov['coverage_status'] == 'needs_retry'
    assert cov['parsed_post_count'] == cov['date_rejected_count'] == 1
    assert collector.search_run_needs_retry(cov)


def test_page_checkpoint_precedes_network_failure(monkeypatch, tmp_path):
    class Request:
        method = 'GET'
        post_data = None
        def all_headers(self): return {}
    class Response:
        status = 200
        request = Request()
        url = 'https://x.com/i/api/graphql/hash/SearchTimeline'
        def json(self): return {'data': {}}
        def all_headers(self): return {}
        def header_value(self, name): return None
    class Pending:
        value = Response()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Page:
        def expect_response(self, *args, **kwargs): return Pending()
        def goto(self, *args, **kwargs): pass
    monkeypatch.setattr(collector, 'parse_search_timeline', lambda *args: ([{'post_id':'1','posted_at':'2026-09-10T05:58:53Z'}], 'cursor-1'))
    monkeypatch.setattr(collector.time, 'sleep', lambda *args: None)
    def fail(*args, **kwargs): raise RuntimeError('response body unavailable')
    monkeypatch.setattr(collector, '_next_browser_signed_search_response', fail)
    saved = []
    with pytest.raises(RuntimeError, match='response body unavailable'):
        collector.collect_search_graphql(Page(), 'GPT', 1, 0, 8, tmp_path, date(2026,9,10), date(2026,9,10), checkpoint=lambda rows, progress: saved.append((list(rows), dict(progress))))
    assert saved[0][0][0]['post_id'] == '1'
    assert saved[0][1]['last_cursor'] == 'cursor-1'
    assert saved[0][1]['stop_reason'] == 'in_progress'


def test_saved_interruption_is_partial_not_automatic_repair():
    runs = [dict(keyword='GPT', slice_start='2026-01-01', slice_end='2026-01-01', count=20, stop_reason='interrupted_with_saved_sample', coverage_status='needs_retry')]
    assert not collector.search_repair_queue(runs, 'coverage_first')
    assert not collector.reliable_search_keys(runs)
    assert ('GPT', '2026-01-01', '2026-01-01') in collector.accepted_search_keys(runs)


def test_failed_response_preserves_server_reset(monkeypatch):
    class Response:
        status = 200
        def json(self): raise RuntimeError('body missing')
    monkeypatch.setattr(collector, '_response_headers', lambda response: {'x-rate-limit-reset': '1300'})
    monkeypatch.setattr(collector.time, 'time', lambda: 1000)
    with pytest.raises(RuntimeError) as failure:
        collector.checked_search_response(Response())
    assert failure.value.search_retry_after == 300


def test_quality_hold_prevents_browser_launch(tmp_path):
    from argparse import Namespace
    (tmp_path/'raw.json').write_text(json.dumps({'requested_keywords':['GPT'], 'guard_hold':{'reason':'date_filter_mismatch'}}),encoding='utf-8')
    with pytest.raises(RuntimeError, match='禁止自动恢复'):
        collector.collect(Namespace(start_date='2026-01-01',end_date='2026-01-01',resume=True),['GPT'],tmp_path)


def test_search_payload_errors_are_not_empty_results() -> None:
    collector.validate_search_payload({"data": {}})
    with pytest.raises(RuntimeError, match="code=32"):
        collector.validate_search_payload({"errors": [{"code": 32}]})
    with pytest.raises(collector.SearchRateLimitError):
        collector.validate_search_payload({"errors": [{"code": 88}]})


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
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    assert query == f"AI policy since_time:{int(datetime(2026,1,2,tzinfo=tz).timestamp())} until_time:{int(datetime(2026,1,6,tzinfo=tz).timestamp())}"
    assert collector.build_search_query('deepseek', date(2026,9,10),date(2026,9,10)) == 'deepseek since_time:1788969600 until_time:1789056000'
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
    assert "重复/无新增/超时不算到底" in report
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

    assert len(queue) == 3
    assert queue[0]["keyword"] == "GPT"
    assert queue[0]["slice_start"] == "2026-01-01"
    assert queue[0]["attempts"] == 1
    assert queue[0]["last_stop_reason"] == "no_network_response"
    assert queue[1]["slice_start"] == "2026-01-02"  # Legacy end label lacks raw proof.
    assert queue[2]["slice_start"] == "2026-01-03"
    assert queue[2]["last_stop_reason"] == "pagination_not_triggered"
    assert collector.reliable_search_keys(runs) == set()

