import json
import sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import x_recovery_policy as p
from x_rollout_recovery_policy import safe_boundary

@pytest.mark.parametrize('node',list('abcde'))
def test_policy_uniform_for_all_accounts(tmp_path,node):
    file=tmp_path/'recovery_policy.json'
    file.write_text(json.dumps({'policy_id':p.POLICY_ID,'enabled_accounts':list('abcde'),'max_404_verifications_per_slice':1}))
    assert p.enabled_by_settings(file,node)
    assert p.policy_receipt(node,True)['max_404_verifications_per_slice']==1

@pytest.mark.parametrize('bad',[{},[],{'policy_id':'wrong','enabled_accounts':['a'],'max_404_verifications_per_slice':1},
    {'policy_id':p.POLICY_ID,'enabled_accounts':['x'],'max_404_verifications_per_slice':1},
    {'policy_id':p.POLICY_ID,'enabled_accounts':['a','a'],'max_404_verifications_per_slice':1},
    {'policy_id':p.POLICY_ID,'enabled_accounts':list('abcde'),'max_404_verifications_per_slice':2}])
def test_invalid_policy_not_silently_expanded(tmp_path,bad):
    file=tmp_path/'bad.json';file.write_text(json.dumps(bad))
    with pytest.raises(ValueError):p.enabled_by_settings(file,'a')

def test_404_budget_and_other_protections():
    record={'count':40,'json_pages_used':2,'last_cursor':'c2','date_rejected_count':0}
    assert p.may_verify_404(True,RuntimeError('HTTP 404'),record,0,3)
    assert not p.may_verify_404(False,RuntimeError('HTTP 404'),record,0,3)
    assert not p.may_verify_404(True,RuntimeError('HTTP 404'),record,3,3)
    for exc in [RuntimeError('HTTP 401 with previous HTTP 404'),RuntimeError('HTTP 403'),
                OSError('disk HTTP 404'),ValueError('HTTP 404'),RuntimeError('HTTP 404 date_filter_mismatch')]:
        assert not p.may_verify_404(True,exc,record,0,3)
    for extra in [{'guard_hold':{'error':'manual'}},{'date_rejected_count':1},{'last_cursor':''},
                  {'automatic_404_verifications':[{'verified':True}]}]:
        assert not p.may_verify_404(True,RuntimeError('HTTP 404'),{**record,**extra},0,3)
    assert not p.may_verify_404(True,RuntimeError('HTTP 404'),{**record,'count':0},0,3)

def test_retry_after_not_shortened(monkeypatch):
    exc=RuntimeError('429');exc.retry_after=900;exc.search_retry_after=1200
    monkeypatch.setattr(p.random,'uniform',lambda a,b:a)
    assert p.backoff_seconds(exc,{'backoff_min':180,'backoff_max':420})>=1200
    assert not p.protected_error(RuntimeError('HTTP 429'))
    for code in [400,401,403,404]:assert p.protected_error(RuntimeError(f'HTTP {code}'))

@pytest.mark.parametrize('fault',['good','in_progress','guard','stale','different_day','cooldown','already_finalized'])
def test_rollout_only_at_saved_date_boundary(fault):
    now=datetime.now(timezone(timedelta(hours=8)))
    raw={'current_date':'2026-02-12','search_runs':[{'slice_start':'2026-02-12','completed_at':(now-timedelta(seconds=1)).isoformat()}]}
    status={'current_date':'2026-02-12','active_phase':'searching'}
    if fault=='in_progress':raw['search_runs'][-1].pop('completed_at')
    elif fault=='guard':raw['guard_hold']={'error':'HTTP 404'}
    elif fault=='stale':raw['search_runs'][-1]['completed_at']=(now-timedelta(seconds=20)).isoformat()
    elif fault=='different_day':raw['current_date']='2026-02-13'
    elif fault=='cooldown':status['active_phase']='cooldown'
    elif fault=='already_finalized':raw['finalized']=True
    assert safe_boundary(raw,status,{'delay_min':15},now)==(fault=='good')
