import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import x_keyword_collector as c

def payload(cursor,rows=None):
    return {'rows':rows or [],'data':{'search_by_raw_query':{'search_timeline':{'timeline':{'instructions':[
        {'type':'TimelineReplaceEntry','entry':{'content':{'cursorType':'Bottom','value':cursor}}}
    ]}}}}}

def test_page_100_continues_101_preflight_not_added(tmp_path,monkeypatch):
    day=date(2026,6,25); query=c.build_search_query('gpt',day,day)
    source={'query':query,'json_pages_used':100,'last_cursor':'saved_100','search_type':'live'}
    seed=[{'post_id':'prior','posted_at':'2026-06-25T00:00:00Z'}]
    pages=[payload('preflight',[{'post_id':'do_not_save','posted_at':'2026-06-25T01:00:00Z'}]),
           payload('c101',[{'post_id':'new','posted_at':'2026-06-25T00:00:01Z'}]),
           payload('c102'),payload('c103'),payload('c104')]
    cursors=['','saved_100','c101','c102','c103']
    class Response:
        status=200
        def __init__(self,index):
            self.url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':cursors[index]})})
            self.request=SimpleNamespace(post_data=None)
            self.data=pages[index]
        def header_value(self,name): return ''
        def json(self): return self.data
    responses=[Response(i) for i in range(len(pages))]
    class Pending:
        value=responses[0]
        def __enter__(self): return self
        def __exit__(self,*args): return False
    class Page:
        def expect_response(self,*args,**kwargs): return Pending()
        def goto(self,*args,**kwargs): pass
    requested=[]
    following=iter(responses[2:]); checkpoints=[]
    monkeypatch.setattr(c,'_fresh_browser_cursor_response',lambda p,q,cursor,b:requested.append(cursor) or responses[1])
    monkeypatch.setattr(c,'_next_browser_signed_search_response',lambda *a,**kw:(next(following),1))
    monkeypatch.setattr(c,'parse_search_timeline',lambda p,*a:(p['rows'],c._bottom_cursor(p)))
    monkeypatch.setattr(c,'_search_template',lambda r:{'url':r.url,'method':'GET'})
    monkeypatch.setattr(c.time,'sleep',lambda _:None)
    rows,result=c._collect_search_graphql_impl(Page(),'gpt',1,0,1000,tmp_path,day,day,no_growth_stop=8,
        resume_record=source,seed_rows=seed,checkpoint=lambda rows,progress:checkpoints.append(dict(progress)))
    assert requested==['saved_100']
    assert [r['post_id'] for r in rows]==['prior','new']
    assert checkpoints[0]['json_pages_used']==101
    assert result['json_pages_used']==104 and result['resumed_from_page']==100
    assert result['continuation_new_unique_posts']==1 and c.search_end_verified(result)
    assert not result['prior_chain_verified'] and not result['natural_day_complete_proven']
    manifest=json.loads(Path(result['continuation_response_manifest']).read_text(encoding='utf-8'))
    assert manifest[0]['request_cursor']=='saved_100' and manifest[0]['page']==101
    assert len(manifest)==4 and all('do_not_save' not in r['parsed_post_ids'] for r in manifest)

@pytest.mark.parametrize('fault',['wrong_query','wrong_sort','missing_cursor','no_seed','budget','out_of_date','guard','date_rejected'])
def test_invalid_resume_never_requests_page_one(tmp_path,fault):
    day=date(2026,6,25); query=c.build_search_query('gpt',day,day)
    record={'query':query,'json_pages_used':100,'last_cursor':'saved_100','search_type':'live'}
    seed=[{'post_id':'prior','posted_at':'2026-06-25T00:00:00Z'}]
    if fault=='wrong_query':record['query']='gpt since:2026-06-25 until:2026-06-25'
    elif fault=='wrong_sort':record['search_type']='top'
    elif fault=='missing_cursor':record['last_cursor']=''
    elif fault=='no_seed':seed=[]
    elif fault=='budget':record['json_pages_used']=1000
    elif fault=='out_of_date':seed[0]['posted_at']='2026-06-26T16:00:00Z'
    elif fault=='guard':record['guard_hold']={'error':'hold'}
    elif fault=='date_rejected':record['date_rejected_count']=1
    with pytest.raises(ValueError):
        c._collect_search_graphql_impl(object(),'gpt',1,0,1000,tmp_path,day,day,resume_record=record,seed_rows=seed)
