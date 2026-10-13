import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import x_keyword_collector as c
import x_apply_404_recovery as recovery

@pytest.fixture
def evidence(tmp_path,monkeypatch):
    day=date(2026,3,2); query=c.build_search_query('GPT',day,day)
    source=tmp_path/'collection/continuation_raw.json'; source.parent.mkdir()
    hold={'at':c.now_iso(),'error':'RuntimeError: SearchTimeline HTTP 404'}
    raw={'account_node':'a','guard_hold':hold,'search_runs':[{'keyword':'GPT','query':query,'slice_start':day.isoformat(),
        'json_pages_used':62,'last_cursor':'saved_62','date_rejected_count':0}], 'posts':[]}
    c._write_json_atomic(source,raw)
    proof={'account_node':'a','state':'404_recovery_verified','held_guard':hold,'source_path':str(source),
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'finished_at':c.now_iso(),
        'query':query,'search_type':'live','from_page':62,'saved_cursor':'saved_62'}
    def response(name,post_id,request_cursor,next_cursor):
        body={'data':{},'rows':[{'post_id':post_id,'posted_at':'2026-03-02T00:00:00Z'}],'next':next_cursor}
        path=tmp_path/(name+'.json');c._write_json_atomic(path,body)
        return {'response_file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'http_status':200,'query':query,'request_cursor_matches':True,'date_rejected_count':0,
                'request_cursor':request_cursor,'next_cursor':next_cursor,'parsed_count':1}
    proof['preflight']=response('preflight','head','','head_cursor')
    proof['continuation']=response('continuation','new','saved_62','saved_63');proof['continuation']['page']=63
    proof['control']=response('control','anchor','previous_61','saved_62')
    original=response('original','anchor','previous_61','saved_62')
    directory=source.parent/'search_end_evidence/anchor';directory.mkdir(parents=True)
    c._write_json_atomic(directory/'responses_manifest.json',[{**original,'page':62}])
    path=tmp_path/'proof.json';c._write_json_atomic(path,proof)
    monkeypatch.setattr(c,'parse_search_timeline',lambda p,*args:(p['rows'],p['next']))
    return path,source,proof

def test_specific_guard_verified_without_changing_source(evidence):
    path,source,proof=evidence;before=source.read_bytes()
    raw,checked,rows,_=recovery.verify(path,source)
    assert rows[0]['post_id']=='new' and checked['continuation']['page']==63
    assert source.read_bytes()==before and raw['guard_hold']==proof['held_guard']

@pytest.mark.parametrize('fault',['other_guard','source_changed','stale','wrong_query','wrong_cursor','http_error','date_rejected','tampered_body','control_ids'])
def test_other_guards_or_bad_proof_never_clear(evidence,fault):
    path,source,proof=evidence
    if fault=='other_guard':
        raw=json.loads(source.read_text());raw['guard_hold']['error']='HTTP 401';c._write_json_atomic(source,raw)
    elif fault=='source_changed':source.write_bytes(source.read_bytes()+b'\n')
    elif fault=='stale':proof['finished_at']='2000-01-01T00:00:00+08:00'
    elif fault=='wrong_query':proof['query']='gpt wrong date'
    elif fault=='wrong_cursor':proof['continuation']['request_cursor']='wrong'
    elif fault=='http_error':proof['continuation']['http_status']=403
    elif fault=='date_rejected':proof['continuation']['date_rejected_count']=1
    elif fault=='tampered_body':Path(proof['continuation']['response_file']).write_text('{}')
    elif fault=='control_ids':
        body_path=Path(proof['control']['response_file']);body=json.loads(body_path.read_text());body['rows'][0]['post_id']='different'
        c._write_json_atomic(body_path,body);proof['control']['sha256']=hashlib.sha256(body_path.read_bytes()).hexdigest()
    c._write_json_atomic(path,proof);before=source.read_bytes()
    with pytest.raises(ValueError):recovery.verify(path,source)
    assert source.read_bytes()==before

def test_http_failure_archive_keeps_body_headers_cursor_not_auth(tmp_path):
    query='gpt';url='https://x.com/i/api/graphql/hash/SearchTimeline?'+urlencode({'variables':json.dumps({'rawQuery':query,'cursor':'c62'})})
    response=SimpleNamespace(status=404,url=url,request=SimpleNamespace(post_data=None),
        json=lambda:{'errors':[{'code':404,'message':'failure'}]},header_value=lambda name:'12345' if name=='x-rate-limit-reset' else '')
    cached=c._BufferedSearchResponse(response)
    with pytest.raises(RuntimeError,match='HTTP 404'):
        c.checked_search_response(cached,tmp_path,query,'c62')
    saved=json.loads(next(tmp_path.glob('http_404_*.json')).read_text())
    assert saved['body']['errors'][0]['code']==404 and saved['request_cursor']=='c62'
    assert saved['expected_cursor']=='c62' and saved['rate_limit_headers']['x-rate-limit-reset']=='12345'
    assert 'authorization' not in json.dumps(saved).lower()
