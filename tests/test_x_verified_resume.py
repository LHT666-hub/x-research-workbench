"""Resume authorization is narrower than clearing arbitrary guard_hold."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from x_workbench.resume import PAUSE_CLOSE_ERROR,validate_pause_resume


def sample():
    raw={'plan_sha256':'plan','posts':[{'post_id':'1'},{'post_id':'2'}],'guard_hold':None,'finalized':False}
    receipt={'ctrl_c_sent':True,'stopped_verified':True,'all_pre_pause_posts_preserved':True,'saved_posts':2,'guard_hold_preserved':None}
    return raw,receipt


@pytest.mark.parametrize('manual_hold',[False,True])
def test_only_verified_pause_can_resume_without_mutation(manual_hold):
    raw,receipt=sample()
    if manual_hold:
        raw['guard_hold']={'at':'saved-pause-time','error':PAUSE_CLOSE_ERROR,'reason':'independent_continuation_requires_review'}
        receipt['guard_hold_preserved']=copy.deepcopy(raw['guard_hold'])
    before=copy.deepcopy(raw)
    validate_pause_resume(raw,receipt,'same','same','plan')
    assert raw==before


@pytest.mark.parametrize('fault',['business_hold','new_hold','changed_checkpoint','plan','incomplete_pause','duplicates','missing_id','count','finalized'])
def test_resume_never_bypasses_new_or_unknown_protection(fault):
    raw,receipt=sample();sha='same'
    if fault=='business_hold':
        raw['guard_hold']={'error':'SearchTimeline HTTP 401','reason':'business'}
        receipt['guard_hold_preserved']=copy.deepcopy(raw['guard_hold'])
    elif fault=='new_hold':raw['guard_hold']={'error':PAUSE_CLOSE_ERROR}
    elif fault=='changed_checkpoint':sha='changed'
    elif fault=='plan':raw['plan_sha256']='changed'
    elif fault=='incomplete_pause':receipt['stopped_verified']=False
    elif fault=='duplicates':raw['posts'][1]['post_id']='1'
    elif fault=='missing_id':raw['posts'][1]={}
    elif fault=='count':receipt['saved_posts']=3
    else:raw['finalized']=True
    with pytest.raises(ValueError):validate_pause_resume(raw,receipt,sha,'same','plan')
