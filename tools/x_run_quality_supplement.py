"""Consume the explicitly authorized D list into independent durable outputs.

Never changes original raw/config/queues or fetches author locations.
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import time
from datetime import date
import x_keyword_collector as c

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run-id',required=True); p.add_argument('--account-node',required=True)
    p.add_argument('--search-engine',choices=['graphql'],required=True)
    p.add_argument('--author-strategy',choices=['deferred'],required=True)
    p.add_argument('--pagination-trigger-attempts',type=int,default=3)
    p.add_argument('--pagination-trigger-timeout-ms',type=int,default=15000)
    p.add_argument('--plan',type=c.Path)
    args=p.parse_args()
    from x_export_column_txt import RUNS
    assert RUNS[args.account_node.upper()]==args.run_id
    assert args.pagination_trigger_attempts==3 and args.pagination_trigger_timeout_ms==15000
    original=c.ROOT/'outputs/x_keyword_runs'/args.run_id
    raw_path=original/'raw.json'; source=raw_path.read_bytes(); raw=json.loads(source.decode('utf-8-sig'))
    cfg=json.loads((original/'run_config.json').read_text(encoding='utf-8-sig'))
    assert cfg['repair_policy']=='coverage_first' and cfg['search_engine']=='graphql' and cfg['author_strategy']=='deferred'
    if raw.get('guard_hold'): raise RuntimeError('原批次有guard_hold，禁止启动')
    plan_path=args.plan or c.ROOT/'outputs/guard_audit/d_supplement_20261006/supplement_queue.json'
    plan=json.loads(plan_path.read_text(encoding='utf-8-sig'))
    assert plan['account_node']==args.account_node
    session=c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if args.account_node=='a' else '-'+args.account_node))
    command="Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^python' -and $_.CommandLine -match 'x_keyword_collector.py|x_probe_d_pagination.py|x_run_quality_supplement.py' -and ($_.CommandLine -like '*"+args.run_id+"*' -or $_.CommandLine -like '*--account-node "+args.account_node+"*') -and $_.ProcessId -notin @("+str(os.getpid())+","+str(os.getppid())+")) -or ($_.Name -eq 'chrome.exe' -and $_.CommandLine -like '*--user-data-dir=*"+str(session)+"*') } | Select-Object -ExpandProperty ProcessId"
    if args.account_node=='a':
        command=command.replace("$_.Name -eq 'chrome.exe' -and", "$_.Name -eq 'chrome.exe' -and $_.CommandLine -notmatch 'chrome-x-keyword-profile-[bcde]' -and")
    proc=subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,check=True)
    if proc.stdout.strip(): raise RuntimeError('已有采集/验证进程或D浏览器；不重复启动')
    out=plan_path.parent/'collection' if args.plan else c.ROOT/'outputs/quality_iteration_20261008/d_supplement'; out.mkdir(parents=True,exist_ok=True)
    path=out/'supplement_raw.json'
    payload=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'original_run_id':args.run_id,'account_node':args.account_node,'source_raw_sha256':hashlib.sha256(source).hexdigest(),'authorized_date_count':len(plan['queue']),'posts':[],'search_runs':[],'finalized':False,'repair_policy':'coverage_first','author_strategy':'deferred','search_engine':'graphql','author_lookup_performed':False}
    if payload.get('guard_hold'): raise RuntimeError('补采guard_hold，需人工核验，不自动恢复')
    assert payload['source_raw_sha256']==hashlib.sha256(source).hexdigest()
    baseline={}
    if plan.get('supplement_baseline'):
        baseline_path=c.ROOT/plan['supplement_baseline']
        if baseline_path.exists():
            baseline=json.loads(baseline_path.read_text(encoding='utf-8-sig'))
            if baseline.get('guard_hold'): raise RuntimeError('先前补采guard_hold，禁止自动继续')
            payload['baseline_supplement_sha256']=hashlib.sha256(baseline_path.read_bytes()).hexdigest()
    seen={str(post.get('post_id') or post.get('post_url')) for post in raw['posts']+baseline.get('posts',[])+payload['posts']}
    authorized_dates={item['slice_start'] for item in plan['queue']}
    finished={r['slice_start'] for r in baseline.get('search_runs',[])+payload['search_runs'] if r.get('completed_at') and c.search_end_verified(r) and r['slice_start'] in authorized_dates}
    def save():
        c._write_json_atomic(path,payload)
        records=baseline.get('search_runs',[])+payload['search_runs']
        cursor_days={r['slice_start'] for r in records if r.get('completed_at') and r.get('stop_reason')=='cursor_exhausted' and r['slice_start'] in authorized_dates}
        tail_days={r['slice_start'] for r in records if r.get('completed_at') and r.get('stop_reason')=='validated_empty_tail' and c.validated_empty_tail(r) and r['slice_start'] in authorized_dates}
        c._write_json_atomic(out/'status.json',{'updated_at':c.now_iso(),'run_id':args.run_id,'account_node':args.account_node,'actual_new_posts':len(payload['posts']),'finished_cursor_days':len(cursor_days),'finished_validated_empty_tail_days':len(tail_days),'finished_search_days':len(finished),'authorized_days':len(plan['queue']),'current_date':payload.get('current_date'),'stage':payload.get('stage'),'active_phase':payload.get('active_phase'),'finalized':payload.get('finalized'),'guard_hold':payload.get('guard_hold'),'original_raw_unchanged':hashlib.sha256(raw_path.read_bytes()).hexdigest()==payload['source_raw_sha256']})
    payload['active_phase']='starting'; save()
    try:
        with c.sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(session),headless=False,executable_path=str(c.CHROME),locale='en-US',viewport={'width':1400,'height':1000})
            try:
                if 'auth_token' not in {cookie['name'] for cookie in context.cookies()}:
                    raise RuntimeError('需要用户重新登录D')
                page=context.pages[0] if context.pages else context.new_page()
                processed=0
                for item in plan['queue']:
                    day=item['slice_start']
                    if item.get('stage')=='2025' and any(r['slice_start'] not in finished for r in plan['queue'] if r.get('stage')=='2026'):
                        payload['active_phase']='2026_needs_review_2025_not_started'
                        print('[阶段保护] 2026仍有未达到接口游标结束的片，不进入2025。',flush=True)
                        break
                    if day in finished: continue
                    if json.loads(raw_path.read_text(encoding='utf-8-sig')).get('guard_hold'):
                        raise RuntimeError('原D新增guard_hold，禁止继续')
                    if processed:
                        time.sleep(random.uniform(cfg['delay_min'],cfg['delay_max']))
                    if processed and processed%cfg['break_every']==0:
                        time.sleep(random.uniform(cfg['break_min'],cfg['break_max']))
                    payload.update({'current_date':day,'stage':item.get('stage','2026'),'active_phase':'searching','finalized':False}); save()
                    print(f'[定向补采] {args.account_node.upper()} {item["keyword"]} {day}；原数据保留，按ID/URL去重。',flush=True)
                    record={'keyword':item['keyword'],'slice_start':day,'slice_end':day,'started_at':c.now_iso(),'stop_reason':'in_progress','new_saved_posts':0}
                    payload['search_runs'].append(record)
                    def checkpoint(rows,progress):
                        for post in rows:
                            key=str(post.get('post_id') or post.get('post_url'))
                            if key and key not in seen:
                                payload['posts'].append(post); seen.add(key); record['new_saved_posts']+=1
                        record.update(progress); record['checkpoint_at']=c.now_iso(); save()
                        print(f"[补采实际落盘] 日期 {day}，本片新增 {record['new_saved_posts']}，补采累计 {len(payload['posts'])}。",flush=True)
                    for attempt in range(cfg['retry_limit']+1):
                        try:
                            rows,coverage=c.collect_search_graphql(page,item['keyword'],1,0,cfg['max_scrolls'],out,date.fromisoformat(day),date.fromisoformat(day),search_type=cfg['search_type'],no_growth_stop=cfg['no_growth_stop'],request_delay_min=cfg['graphql_delay_min'],request_delay_max=cfg['graphql_delay_max'],timezone_offset=cfg['timezone_offset'],pagination_trigger_attempts=3,pagination_trigger_timeout_ms=15000,checkpoint=checkpoint)
                            record.update(coverage); record['completed_at']=c.now_iso()
                            if c.search_end_verified(coverage): finished.add(day)
                            elif coverage['stop_reason']=='date_filter_mismatch': raise RuntimeError('date_filter_mismatch；需人工核验')
                            save(); print(f"[本片结束] {day} {coverage['stop_reason']}；新增 {record['new_saved_posts']}，未把技术停止算完成。",flush=True)
                            break
                        except Exception as exc:
                            record['error']=str(exc); save()
                            if isinstance(exc,OSError) or any(s in str(exc) for s in ['HTTP 401','HTTP 403','HTTP 404','登录','date_filter_mismatch']): raise
                            pause=max(random.uniform(cfg['backoff_min'],cfg['backoff_max'])*(attempt+1), (getattr(exc,'retry_after',None) or getattr(exc,'search_retry_after',0) or 0)+random.uniform(10,45))
                            payload.update({'active_phase':'cooldown','cooldown_until_epoch':time.time()+pause}); save()
                            print(f'[退避] {type(exc).__name__}: {exc}；等待 {pause/60:.1f} 分钟，不加速。',flush=True)
                            time.sleep(pause)
                            if attempt>=cfg['retry_limit']: raise
                            payload['active_phase']='searching'
                    processed+=1
                payload['current_date']=None
                payload['finalized']=len(finished)==len(plan['queue'])
                payload['active_phase']='completed' if payload['finalized'] else 'supplement_needs_review'
            finally:
                context.close()
    except Exception as exc:
        payload['active_phase']='paused_needs_review'; payload['finalized']=False
        payload['guard_hold']={'detected_at':c.now_iso(),'error':str(exc),'reason':'supplement_error_requires_review'}
        print(f'[补采保护暂停] {exc}',flush=True)
    finally:
        save()
        if payload['posts']:
            from x_export_column_txt import export_rows
            export_rows(payload['posts'],args.account_node.upper(),out/(args.account_node.upper()+'_new_posts.txt'))
        print(json.dumps({'state':payload['active_phase'],'new_posts':len(payload['posts']),'verified_search_end_days':len(finished),'authorized_days':len(plan['queue'])},ensure_ascii=False),flush=True)

if __name__=='__main__': main()
