"""Shared bounded recovery rules; no profile calls or source-state mutation."""
import random
import re
import json

POLICY_ID='verified_404_v2'
ACCOUNTS=frozenset('abcde')
MAX_404_VERIFICATIONS_PER_SLICE=1

def protected_error(exc):
    return isinstance(exc,(OSError,ValueError)) or bool(re.search(
        r'HTTP\s*(400|401|403|404)|登录|requires login|captcha|验证码|date_filter_mismatch|日期|查询.*不匹配|游标.*不匹配',
        str(exc),re.I))

def may_verify_404(enabled,exc,record,attempt,retry_limit):
    strong=re.search(r'HTTP\s*(400|401|403)|登录|requires login|captcha|验证码|date_filter_mismatch|日期|查询.*不匹配|游标.*不匹配',str(exc),re.I)
    return bool(enabled and re.search(r'HTTP\s*404',str(exc),re.I) and not strong
        and not isinstance(exc,(OSError,ValueError))
        and not record.get('guard_hold') and int(record.get('date_rejected_count',0) or 0)==0
        and int(record.get('count') or record.get('baseline_saved_posts') or record.get('source_attempt_count') or 0)>0
        and record.get('last_cursor') and int(record.get('json_pages_used',0) or 0)>0
        and len(record.get('automatic_404_verifications',[]))<MAX_404_VERIFICATIONS_PER_SLICE
        and attempt<retry_limit)

def backoff_seconds(exc,cfg,attempt=0):
    explicit=max(float(getattr(exc,'retry_after',0) or 0),float(getattr(exc,'search_retry_after',0) or 0))
    return max(random.uniform(cfg['backoff_min'],cfg['backoff_max'])*(attempt+1),explicit+random.uniform(10,45))

def policy_receipt(node,enabled):
    if node not in ACCOUNTS:raise ValueError('未知账号节点')
    return {'id':POLICY_ID,'account_node':node,'enabled':bool(enabled),
            'max_404_verifications_per_slice':MAX_404_VERIFICATIONS_PER_SLICE,
            'fresh_query_cursor_and_control_required':True,'first_page_added_to_dataset':False,
            'retry_limits_from_original_config':True,'auth_date_storage_guards_preserved':True}

def enabled_by_settings(path,node):
    if node not in ACCOUNTS:raise ValueError('未知账号节点')
    if not path.exists():return False
    settings=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(settings,dict):raise ValueError('恢复策略不是JSON对象')
    accounts=settings.get('enabled_accounts')
    if (settings.get('policy_id')!=POLICY_ID or settings.get('max_404_verifications_per_slice')!=1
        or not isinstance(accounts,list) or not all(isinstance(n,str) and n in ACCOUNTS for n in accounts)
        or len(accounts)!=len(set(accounts))):
        raise ValueError('恢复策略配置不兼容，禁止扩大重试或账号范围')
    return node in accounts
