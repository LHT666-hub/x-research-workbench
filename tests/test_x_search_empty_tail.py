import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import x_keyword_collector as c
import x_guard_audit as audit
import x_keyword_ui as ui


def cursor_payload(cursor):
    return {'data': {'search_by_raw_query': {'search_timeline': {'timeline': {'instructions': [
        {'type':'TimelineReplaceEntry','entry':{'content':{'cursorType':'Bottom','value':cursor}}}
    ]}}}}}


def verified_run(tmp_path):
    query=c.build_search_query('gpt',date(2026,6,25),date(2026,6,25))
    responses=[]
    for index in range(3):
        path=tmp_path/f'empty_{index}.json'
        c._write_json_atomic(path,cursor_payload(f'cursor_{index+1}'))
        responses.append({'http_status':200,'query':query,'request_cursor_matches':True,
            'request_cursor':f'cursor_{index}','next_cursor':f'cursor_{index+1}',
            'parsed_count':0,'response_file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'keyword':'gpt','slice_start':'2026-06-25','slice_end':'2026-06-25',
        'query':query,'count':10,'date_rejected_count':0,'stop_reason':'validated_empty_tail',
        'coverage_status':'search_tail_verified','search_end_evidence':{'policy':c.EMPTY_TAIL_POLICY,'responses':responses}}


def test_tail_reused_by_retry_ui_and_audit(tmp_path):
    run=verified_run(tmp_path)
    assert c.validated_empty_tail(run) and c.search_end_verified(run)
    assert not c.search_run_needs_retry(run) and not ui._search_run_needs_retry(run)
    interrupted={**run,'stop_reason':'pagination_not_triggered','coverage_status':'needs_retry'}
    assert c.search_repair_queue([interrupted,run],policy='strict')==[]
    ledger=audit.coverage({'start_date':'2026-06-25','end_date':'2026-06-25'},['gpt'],[run])
    assert ledger[0]['validated_empty_tail'] and ledger[0]['state']=='reliable_nonzero'


@pytest.mark.parametrize('fault',['one_empty','wrong_query','wrong_cursor','http_error','parsed_posts','date_rejected','missing_file','duplicate_evidence','tampered_file','no_samples'])
def test_bad_evidence_never_clears_retry(tmp_path,fault):
    run=verified_run(tmp_path)
    pages=run['search_end_evidence']['responses']
    if fault=='one_empty': del pages[1:]
    elif fault=='wrong_query': pages[1]['query']='different day'
    elif fault=='wrong_cursor': pages[1]['request_cursor']='wrong'
    elif fault=='http_error': pages[1]['http_status']=429
    elif fault=='parsed_posts': pages[1]['parsed_count']=20
    elif fault=='date_rejected': run['date_rejected_count']=1
    elif fault=='missing_file': pages[1]['response_file']=str(tmp_path/'missing.json')
    elif fault=='duplicate_evidence': pages[1]=pages[0]
    elif fault=='tampered_file': Path(pages[1]['response_file']).write_text('{}',encoding='utf-8')
    elif fault=='no_samples': run['count']=0
    assert not c.validated_empty_tail(run)
    assert c.search_run_needs_retry(run) and ui._search_run_needs_retry(run)


def test_tombstone_or_unknown_empty_shape_not_tail():
    assert not c._cursor_only_empty_payload({'data':{}})
    payload=cursor_payload('next')
    payload['data']['search_by_raw_query']['search_timeline']['timeline']['instructions'][0]['entry']['content']={'entryType':'TimelineTimelineItem','tweet_results':{'result':{'__typename':'TweetUnavailable'}}}
    assert not c._cursor_only_empty_payload(payload)


@pytest.mark.parametrize('end_kind',['tail','no_cursor'])
def test_collector_emits_distinct_tail_state_and_raw_evidence(tmp_path,monkeypatch,end_kind):
    query=c.build_search_query('gpt',date(2026,6,25),date(2026,6,25))
    payloads=[cursor_payload(f'cursor_{i+1}') for i in range(4)]
    payloads[0]['rows']=[{'post_id':'1','posted_at':'2026-06-25T00:00:00Z'}]
    if end_kind=='no_cursor':
        payloads[1]={'data':{'search_by_raw_query':{'search_timeline':{'timeline':{'instructions':[{'type':'TimelineTerminateTimeline','direction':'Bottom'}]}}}}}
    class Response:
        status=200
        def __init__(self,index):
            self.url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':'' if index==0 else f'cursor_{index}'})})
            self.request=SimpleNamespace(post_data=None)
            self.payload=payloads[index]
        def header_value(self,name): return ''
        def json(self): return self.payload
    responses=[Response(i) for i in range(4)]
    class Pending:
        value=responses[0]
        def __enter__(self): return self
        def __exit__(self,*args): return False
    class Page:
        def expect_response(self,*args,**kwargs): return Pending()
        def goto(self,*args,**kwargs): pass
    following=iter(responses[1:])
    monkeypatch.setattr(c,'_search_template',lambda r:{'url':r.url,'method':'GET'})
    monkeypatch.setattr(c,'parse_search_timeline',lambda p,*args:(p.get('rows',[]),c._bottom_cursor(p)))
    monkeypatch.setattr(c,'_next_browser_signed_search_response',lambda *args,**kwargs:(next(following),1))
    monkeypatch.setattr(c.time,'sleep',lambda _:None)
    rows,coverage=c._collect_search_graphql_impl(Page(),'gpt',1,0,1000,tmp_path,date(2026,6,25),date(2026,6,25),no_growth_stop=8)
    assert len(rows)==1 and coverage['search_end_verified']
    assert c.search_end_verified(coverage) and not coverage['platform_all_posts_proven']
    if end_kind=='tail':
        assert coverage['stop_reason']=='validated_empty_tail'
        assert coverage['coverage_status']=='search_tail_verified' and coverage['last_cursor']=='cursor_4'
        assert c.validated_empty_tail(coverage)
    else:
        assert coverage['stop_reason']=='cursor_exhausted'
        assert coverage['search_end_status']=='response_end' and c.response_end_evidence_valid(coverage)


@pytest.mark.parametrize('reason,extra,code',[
    ('no_new_results',{'count':200,'coverage_status':'apparently_exhausted'},'no_new_results'),
    ('repeated_cursor',{},'repeated_cursor'),
    ('max_pages',{},'max_pages'),('max_scrolls',{},'max_scrolls'),('target_reached',{},'target_reached'),
    ('pagination_not_triggered',{},'pagination_not_triggered'),
    ('no_network_response',{},'no_network_response'),
    ('interrupted_with_saved_sample',{'interruption_error':'net::ERR_ABORTED'},'request_aborted'),
    ('interrupted_with_saved_sample',{'interruption_error':'Target page, context or browser has been closed'},'browser_disconnected'),
    ('interrupted_with_saved_sample',{},'interrupted_with_saved_sample'),
    ('in_progress',{},'in_progress'),('user_paused',{},'user_paused'),
    ('cooldown',{},'rate_limited'),('http_429',{},'rate_limited'),
    ('error',{'error':'SearchTimeline HTTP 401'},'authentication_hold'),
    ('error',{'error':'SearchTimeline HTTP 403'},'authentication_hold'),
    ('error',{'error':'SearchTimeline HTTP 404'},'endpoint_hold'),
    ('error',{'error':'PermissionError: write failed'},'storage_failure'),
    ('date_filter_mismatch',{},'date_query_invalid'),
    ('cursor_exhausted',{'query':'gpt since:2026-06-25 until:2026-06-25'},'date_query_invalid'),
    ('cursor_exhausted',{},'recorded_cursor_end'),
    ('something_new',{},'unknown_stop'),
    ('validated_empty_tail',{},'invalid_end_evidence'),
])
def test_all_unverified_stops_never_become_end(reason,extra,code):
    run={'stop_reason':reason,**extra}
    result=c.classify_search_run(run)
    assert result['state_code']==code and not result['search_end_verified']
    assert not result['platform_all_posts_proven']


def test_cursor_end_requires_raw_evidence_and_supports_normal_empty(tmp_path):
    path=tmp_path/'end.json'
    payload={'data':{'search_by_raw_query':{'search_timeline':{'timeline':{'instructions':[{'type':'TimelineTerminateTimeline','direction':'Bottom'}]}}}}}
    c._write_json_atomic(path,payload)
    run={'stop_reason':'cursor_exhausted','count':0,'query':'gpt since_time:1782316800 until_time:1782403200',
         'coverage_status':'apparently_exhausted','search_end_evidence':{'policy':'response_end_v1','responses':[{
             'http_status':200,'query':'gpt since_time:1782316800 until_time:1782403200','request_cursor_matches':True,
             'next_cursor':'','response_file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]}}
    assert c.classify_search_run(run)['state_code']=='response_end'
    assert c.search_end_verified(run) and not c.search_run_needs_retry(run)
    run['guard_hold']={'error':'unknown business protection'}
    assert not c.search_end_verified(run)


def test_same_state_available_for_other_dates_without_borrowing_label(tmp_path):
    for day in ('2026-06-24','2026-06-26'):
        directory=tmp_path/day; directory.mkdir()
        run=verified_run(directory)
        query=c.build_search_query('gpt',date.fromisoformat(day),date.fromisoformat(day))
        run.update(query=query,slice_start=day,slice_end=day)
        for page in run['search_end_evidence']['responses']: page['query']=query
        assert c.classify_search_run(run)['state_code']=='validated_empty_tail'
    assert not c.search_end_verified({'stop_reason':'validated_empty_tail','count':100})


def test_guard_keeps_underlying_error_but_never_ends():
    result=c.classify_search_run({'stop_reason':'error','guard_hold':{'error':'net::ERR_ABORTED'}})
    assert result['state_code']=='guard_hold' and result['underlying_state_code']=='request_aborted'
    assert not result['search_end_verified']
