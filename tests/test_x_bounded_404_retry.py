import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import x_keyword_collector as c
import x_continue_saved_cursors as runner

@pytest.mark.parametrize('second_404',[False,True])
def test_bounded_verification_uses_all_durable_samples_not_old_seed(tmp_path,monkeypatch,second_404):
    day='2026-02-12'; query='gemini since_time:1770825600 until_time:1770912000'
    def post(pid):return {'post_id':pid,'posted_at':'2026-02-12T00:00:00Z','keyword':'gemini'}
    baseline={'posts':[post('base')],'guard_hold':None}
    source=tmp_path/'source.json';c._write_json_atomic(source,baseline)
    cfg={'repair_policy':'coverage_first','search_engine':'graphql','author_strategy':'deferred','max_scrolls':1000,
         'search_type':'live','no_growth_stop':8,'graphql_delay_min':2,'graphql_delay_max':4,'timezone_offset':8,
         'delay_min':15,'delay_max':30,'break_every':15,'break_min':180,'break_max':360,
         'retry_limit':3,'backoff_min':180,'backoff_max':420}
    cfg_path=tmp_path/'config.json';c._write_json_atomic(cfg_path,cfg)
    item={'source_path':str(source),'source_record_index':0,'slice_start':day,'slice_end':day,'query':query,
          'keyword':'gemini','source_attempt_count':1,'json_pages_used':2,'last_cursor':'c2','date_rejected_count':0,'search_type':'live'}
    plan={'account_node':'c','run_id':'C','config_path':str(cfg_path),'config_sha256':hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
          'sources':[{'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'manual_pause_verified':True}],'queue':[item]}
    plan_path=tmp_path/'continuation_plan.json';c._write_json_atomic(plan_path,plan)
    calls=[]
    def collect(page,*args,**kwargs):
        record=kwargs['resume_record'];calls.append(record['json_pages_used'])
        if len(calls)==1:
            kwargs['checkpoint']([post('base'),post('page3a'),post('page3b')],{'query':query,'slice_start':day,'slice_end':day,
                'count':3,'json_pages_used':3,'last_cursor':'c3','parsed_post_count':2,'date_rejected_count':0,'stop_reason':'in_progress'})
            raise RuntimeError('SearchTimeline HTTP 404')
        assert record['json_pages_used']==4 and record['last_cursor']=='c4'
        assert record['count']==4
        assert {p['post_id'] for p in kwargs['seed_rows']}=={'base','page3a','page3b','verified_page4'}
        if second_404:raise RuntimeError('SearchTimeline HTTP 404')
        return [],{'query':query,'count':4,'json_pages_used':4,'last_cursor':'c4','stop_reason':'pagination_not_triggered','state_code':'pagination_not_triggered','date_rejected_count':0}
    def verify(page,record,out,cfg,on_state=None):
        saved=json.loads((out/'continuation_raw.json').read_text())
        assert saved['guard_hold']['reason']=='404_pending_bounded_verification'
        assert len(saved['posts'])==2
        return [post('verified_page4')],{'next_cursor':'c4','parsed_count':1},str(out/'test_only_proof.json')
    class Context:
        pages=[object()]
        def cookies(self):return [{'name':'auth_token'}]
        def close(self):pass
    class Playwright:
        chromium=SimpleNamespace(launch_persistent_context=lambda *a,**k:Context())
        def __enter__(self):return self
        def __exit__(self,*args):return False
    monkeypatch.setattr(c,'ROOT',tmp_path)
    monkeypatch.setattr(c,'sync_playwright',lambda:Playwright())
    monkeypatch.setattr(c,'collect_search_graphql',collect)
    monkeypatch.setattr(runner,'verify_transient_404',verify)
    monkeypatch.setattr(runner,'account_busy',lambda *a:'')
    monkeypatch.setattr(c.time,'sleep',lambda *a:None)
    monkeypatch.setattr(sys,'argv',['runner','--account-node','c','--run-id','C','--plan',str(plan_path),
        '--search-engine','graphql','--author-strategy','deferred','--verify-404-once'])
    runner.main()
    saved=json.loads((tmp_path/'collection/continuation_raw.json').read_text())
    assert calls==[2,4]
    assert {p['post_id'] for p in saved['posts']}=={'page3a','page3b','verified_page4'}
    assert len(saved['guard_hold_history'])==1
    if second_404:assert 'HTTP 404' in saved['guard_hold']['error']
    else:assert not saved['guard_hold']
    assert saved['search_runs'][0]['automatic_404_verifications'][0]['verified']
    assert not saved['finalized']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==plan['sources'][0]['sha256']

def test_bounded_flag_does_not_authorize_unknown_accounts(monkeypatch):
    monkeypatch.setattr(sys,'argv',['runner','--account-node','x','--run-id','E','--plan','unused.json',
        '--search-engine','graphql','--author-strategy','deferred','--verify-404-once'])
    with pytest.raises(SystemExit):runner.main()
