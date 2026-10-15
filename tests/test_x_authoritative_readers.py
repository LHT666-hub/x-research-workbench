"""UI/audit/export agree even when the compatibility JSON is behind SQLite."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import threading
from urllib.request import urlopen

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import x_keyword_ui as ui
import x_guard_audit as audit
from x_export_column_txt import export_checkpoint
from x_workbench import continuation_view as view_module
from x_workbench.state import read_state
from x_workbench.storage import SQLiteCheckpoint, write_json_atomic, load_continuation_state


@pytest.fixture
def lagging(tmp_path,monkeypatch):
    root=tmp_path/'continuations';folder=root/'a';folder.mkdir(parents=True)
    plan={'account_node':'a','run_id':'synthetic','queue':[{'keyword':'gpt','source_path':'synthetic-source','source_record_index':0}]}
    write_json_atomic(folder/'continuation_plan.json',plan)
    source=folder/'collection/continuation_raw.json'
    post={'post_id':'1','post_text':'synthetic gpt','keyword':'gpt','posted_at':'2026-06-25T00:00:00Z'}
    state={'account_node':'a','plan_sha256':hashlib.sha256((folder/'continuation_plan.json').read_bytes()).hexdigest(),
           'posts':[post],'search_runs':[],'guard_hold':None,'finalized':False,'active_phase':'searching','current_date':'2026-06-25'}
    store=SQLiteCheckpoint(source)
    store.commit(state);store.export_snapshot(state)
    state['posts'].append({**post,'post_id':'2'})
    state['current_progress']={'page':101}
    store.commit(state)
    assert len(json.loads(source.read_text(encoding='utf-8'))['posts'])==1
    monkeypatch.setattr(ui,'CONTINUATION_ROOT',root)
    monkeypatch.setattr(ui,'ROOT',tmp_path)
    monkeypatch.setattr(view_module,'worker_observations',lambda root:{n:{'worker_alive':False,'worker_pids':[],'duplicate_workers':False} for n in 'abcde'})
    monkeypatch.setattr(ui,'worker_observations',view_module.worker_observations)
    try:yield root,source,state,store
    finally:store.close()


@contextmanager
def server():
    service=ui.ThreadingHTTPServer(('127.0.0.1',0),ui.Handler)
    worker=threading.Thread(target=service.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{service.server_address[1]}'
    finally:service.shutdown();service.server_close();worker.join(timeout=3)


def test_full_summary_preview_and_old_loader_share_revision(lagging):
    root,source,state,store=lagging
    summary=read_state(source,preview_limit=0,live=True)
    preview=read_state(source,preview_limit=1,live=True)
    full=read_state(source)
    assert summary.revision==preview.revision==full.revision=='sqlite-2'
    assert summary.post_count==preview.post_count==full.post_count==2
    assert summary.state['posts']==[] and preview.state['posts'][0]['post_id']=='2'
    assert load_continuation_state(source)==state


def test_ui_audit_export_ignore_lagging_json_and_status(lagging,tmp_path):
    root,source,state,store=lagging
    write_json_atomic(source.parent/'status.json',{'actual_new_posts':999})
    report=view_module.audit_continuations(root)[0]
    assert report['actual_new_posts']==2 and report['snapshot']['revision']=='sqlite-2'
    with server() as base:
        summary=json.load(urlopen(base+'/api/continuations',timeout=5))['accounts'][0]
        detail=json.load(urlopen(base+'/api/continuation?node=a',timeout=5))
        response=urlopen(base+'/api/continuation/export?node=a',timeout=5)
        lines=response.read().decode('utf-8-sig').splitlines()
        assert response.headers['X-Checkpoint-Revision']=='sqlite-2'
    assert summary['actual_new_posts']==detail['actual_new_posts']==len(lines)-1==2
    assert len(detail['posts_preview'])==2
    assert detail['current_progress']['page']==101
    assert all(len(line.split('\t'))==12 for line in lines)
    assert not detail['platform_all_posts_proven']


def test_legacy_ui_counts_and_cumulative_do_not_trust_stale_export(tmp_path,monkeypatch):
    folder=tmp_path/'run';folder.mkdir()
    write_json_atomic(folder/'raw.json',{'posts':[{'post_id':'1','keyword':'gpt'}],'search_runs':[]})
    write_json_atomic(folder/'status.json',{'post_count':999})
    write_json_atomic(folder/'research_dataset.json',[{'post_id':'stale'}])
    monkeypatch.setattr(ui,'OUTPUT_ROOT',tmp_path)
    assert ui.history_rows()[0]['post_count']==1
    assert [p['post_id'] for p in ui.cumulative_posts()]==['1']


def test_common_export_receipt_matches_audit_reader(lagging,tmp_path):
    root,source,state,store=lagging
    receipt=export_checkpoint(source,tmp_path/'export.txt','A')
    assert receipt['rows']==len(audit.read_json(source)['posts'])==2
    assert receipt['revision']==read_state(source).revision
    assert receipt['export_sha256']==hashlib.sha256((tmp_path/'export.txt').read_bytes()).hexdigest()


def test_invalid_sidecar_never_silently_falls_back_to_old_json(lagging):
    root,source,state,store=lagging
    write_json_atomic(source,{**state,'guard_hold':{'reason':'manual-business-hold'}})
    with pytest.raises(ValueError):read_state(source)
    with server() as base:
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as exc:urlopen(base+'/api/continuations',timeout=5)
        assert exc.value.code==503


def test_reader_has_no_mutation_of_checkpoint(lagging):
    root,source,state,store=lagging
    before=source.read_bytes()
    generation=store.generation
    for _ in range(3):
        read_state(source,preview_limit=0,live=True)
        read_state(source)
    assert source.read_bytes()==before and store.generation==generation
