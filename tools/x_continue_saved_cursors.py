"""Independent, durable exact-cursor continuation. Never mutates historical runs."""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import time
from datetime import date
import x_keyword_collector as c
from x_plan_cursor_continuation import read,local_day,manual_pause_verified
from x_recovery_policy import POLICY_ID,may_verify_404,backoff_seconds,policy_receipt,protected_error,enabled_by_settings

def account_busy(node,run_id,session):
    pattern=re.escape(str(session)) + (r'(?:["\s]|$)' if node=='a' else '')
    command="""Get-CimInstance Win32_Process | Where-Object {
      ($_.Name -match '^python' -and $_.CommandLine -match 'x_.*(collect|supplement|verify|probe|continu)' -and
       ($_.CommandLine -like '*RUNID*' -or $_.CommandLine -match '--account-node[ =]NODE( |$)') -and
       $_.ProcessId -notin @(SELF,PARENT)) -or
      ($_.Name -eq 'chrome.exe' -and $_.CommandLine -match 'PROFILE')
    } | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"""
    command=command.replace('RUNID',run_id).replace('NODE',node).replace('SELF',str(os.getpid())).replace('PARENT',str(os.getppid())).replace('PROFILE',pattern.replace("'","''"))
    result=subprocess.run(['powershell','-NoProfile','-Command',command],text=True,capture_output=True,check=True)
    return result.stdout.strip()

def fatal(exc):
    return isinstance(exc,(OSError,ValueError)) or bool(re.search(r'HTTP\s*(400|401|403|404)|登录|验证码|date_filter_mismatch|日期核验|查询/游标不匹配',str(exc),re.I))

def verify_transient_404(page,record,out,cfg,on_state=None):
    """One bounded verification, not an unconditional retry or guard bypass."""
    query=record['query']; day=date.fromisoformat(record['slice_start'])
    if query!=c.build_search_query(record['keyword'],day,day,cfg['timezone_offset']):
        raise ValueError('404复核查询不兼容')
    candidates=[]
    for path in out.glob('search_end_evidence/*/responses_manifest.json'):
        entries=json.loads(path.read_text(encoding='utf-8'))
        if entries and entries[-1]['query']==query and entries[-1]['page']==record['json_pages_used'] and entries[-1]['next_cursor']==record['last_cursor']:
            candidates.append(entries[-1])
    if len(candidates)!=1:raise ValueError('404复核不能唯一关联已保存页')
    anchor=candidates[0]; anchor_bytes=c.Path(anchor['response_file']).read_bytes()
    if hashlib.sha256(anchor_bytes).hexdigest()!=anchor['sha256']:raise ValueError('保存页证据哈希不符')
    proof_dir=out/'404_verification'/str(time.time_ns());proof_dir.mkdir(parents=True)
    proof={'policy_id':POLICY_ID,'account_node':cfg.get('account_node'),'search_type':cfg['search_type'],
           'timezone_offset':cfg['timezone_offset'],'query':query,'source_page':record['json_pages_used'],
           'anchor':anchor,'created_at':c.now_iso(),'samples':[],'stage_retries':[]}
    def take(response,cursor,name):
        payload=c.checked_search_response(response,proof_dir/'failures',query,cursor)
        if c._search_raw_query(response.url,response.request.post_data)!=query or c._search_request_cursor(response.url,response.request.post_data)!=cursor:
            raise ValueError('404复核响应查询/游标不匹配')
        rows,next_cursor=c.parse_search_timeline(payload,record['keyword'],1)
        if any(not c.post_in_date_range(r['posted_at'],day,day,cfg['timezone_offset']) for r in rows):
            raise ValueError('404复核日期错配')
        path=proof_dir/f'{name}.json';c._write_json_atomic(path,payload)
        item={'http_status':response.status,'query':query,'request_cursor':cursor,'request_cursor_matches':True,
              'next_cursor':next_cursor or '', 'parsed_count':len(rows),'received_at':c.now_iso(),
              'response_file':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'rate_limit':c._rate_limit_state(c._response_headers(response))}
        proof['samples'].append(item);c._write_json_atomic(proof_dir/'verification.json',proof)
        return rows,item,payload
    def stage(name,cursor,request):
        for stage_attempt in range(cfg['retry_limit']+1):
            try:
                return take(request(),cursor,name)
            except Exception as exc:
                proof['stage_retries'].append({'at':c.now_iso(),'stage':name,'attempt':stage_attempt,
                    'error':f'{type(exc).__name__}: {exc}'})
                c._write_json_atomic(proof_dir/'verification.json',proof)
                if protected_error(exc) or stage_attempt>=cfg['retry_limit']:raise
                pause=backoff_seconds(exc,cfg,stage_attempt)
                print(f'[404复核退避] {name} {exc}，等待{pause/60:.1f}分钟；不将429/超时当复核失败或采完。',flush=True)
                if on_state:on_state('404_verification_cooldown',time.time()+pause)
                time.sleep(pause)
                if on_state:on_state('404_verifying',None)
    def wait(item):
        rate=item['rate_limit'];pause=random.uniform(cfg['graphql_delay_min'],cfg['graphql_delay_max'])
        if rate.get('remaining',999)<=5 and rate.get('reset',0)>time.time():
            pause=max(pause,rate['reset']-time.time()+random.uniform(5,20))
        if pause>cfg['graphql_delay_max'] and on_state:on_state('404_verification_cooldown',time.time()+pause)
        time.sleep(pause)
        if on_state:on_state('404_verifying',None)
    url='https://x.com/search?q='+c.quote(query)+'&src=typed_query'+('&f=live' if cfg['search_type']=='live' else '')
    def first_response():
        with page.expect_response(lambda r:c._search_response_matches(r,query,''),timeout=60000) as pending:
            page.goto(url,wait_until='domcontentloaded',timeout=60000)
        return pending.value
    head,initial,_=stage('preflight','',first_response)
    if not head:raise ValueError('404复核首页无有效样本')
    wait(initial)
    rows,continuation,body=stage('continuation',record['last_cursor'],lambda:c._fresh_browser_cursor_response(page,query,record['last_cursor']))
    if not rows and continuation['next_cursor'] and not c._cursor_only_empty_payload(body):
        raise ValueError('404复核空响应不是规范游标空尾页')
    if not continuation['next_cursor'] and not c.response_end_evidence_valid({
        'query':query,'date_rejected_count':0,'search_end_evidence':{'policy':'response_end_v1','responses':[continuation]}}):
        raise ValueError('404复核无游标响应证据不完整')
    wait(continuation)
    control,_,_=stage('control',anchor['request_cursor'],lambda:c._fresh_browser_cursor_response(page,query,anchor['request_cursor']))
    original,_=c.parse_search_timeline(json.loads(anchor_bytes),record['keyword'],1)
    if {r['post_id'] for r in control}!={r['post_id'] for r in original}:
        raise ValueError('404复核保存页控制组不一致')
    proof.update(verified=True,control_ids_match=True,finished_at=c.now_iso())
    c._write_json_atomic(proof_dir/'verification.json',proof)
    return rows,continuation,str((proof_dir/'verification.json').resolve())

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--account-node',choices=list('abcde'),required=True)
    p.add_argument('--run-id',required=True)
    p.add_argument('--plan',type=c.Path,required=True)
    p.add_argument('--search-engine',choices=['graphql'],required=True)
    p.add_argument('--author-strategy',choices=['deferred'],required=True)
    p.add_argument('--pagination-trigger-attempts',type=int,default=3)
    p.add_argument('--pagination-trigger-timeout-ms',type=int,default=15000)
    p.add_argument('--verify-404-once',action='store_true',help='Explicitly authorized bounded verification; never ignore a 404')
    args=p.parse_args(); node=args.account_node
    settings_path=c.ROOT/'recovery_policy.json'
    args.verify_404_once=enabled_by_settings(settings_path,node) if settings_path.exists() else args.verify_404_once
    assert (args.pagination_trigger_attempts,args.pagination_trigger_timeout_ms)==(3,15000)
    plan,plan_sha=read(args.plan)
    assert plan['account_node']==node and plan['run_id']==args.run_id
    cfg,cfg_sha=read(c.Path(plan['config_path']))
    assert cfg_sha==plan['config_sha256']
    assert cfg['repair_policy']=='coverage_first' and cfg['search_engine']=='graphql' and cfg['author_strategy']=='deferred'
    if cfg.get('account_node',node)!=node:raise ValueError('原配置账号与运行节点不符')
    session=c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if node=='a' else '-'+node))
    if account_busy(node,args.run_id,session): raise RuntimeError('账号已有collector/验证/浏览器；不重复启动')
    sources={}; baseline={}; known=set()
    for receipt in plan['sources']:
        path=c.Path(receipt['path']); raw,sha=read(path)
        if sha!=receipt['sha256']: raise RuntimeError('历史源文件已变化；先重新审计，不启动')
        if not receipt['manual_pause_verified'] or not manual_pause_verified(node,path,raw):
            raise RuntimeError('未核实的guard_hold；禁止请求')
        sources[str(path)]=raw
        for post in raw['posts']:
            key=str(post.get('post_id') or post.get('post_url') or '')
            if key:
                known.add(key); baseline.setdefault(local_day(post),{})[key]=post
    out=args.plan.parent/'collection'; out.mkdir(parents=True,exist_ok=True)
    lock=out/'worker.lock'
    handle=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    os.write(handle,str(os.getpid()).encode()); os.close(handle)
    path=out/'continuation_raw.json'
    payload=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'created_at':c.now_iso(),'account_node':node,'original_run_id':args.run_id,'plan_sha256':plan_sha,
        'sources':plan['sources'],'posts':[],'search_runs':[],'guard_hold':None,'finalized':False,
        'search_engine':'graphql','author_strategy':'deferred','repair_policy':'coverage_first',
        'author_lookup_performed':False,'automation_resumed':False,'platform_all_posts_proven':False}
    if payload['plan_sha256']!=plan_sha or payload.get('guard_hold'):
        lock.unlink(); raise RuntimeError('独立计划改变或存在新保护；禁止恢复')
    known.update(str(r.get('post_id') or r.get('post_url')) for r in payload['posts'])
    payload['recovery_policy']={**policy_receipt(node,args.verify_404_once),'activated_at':c.now_iso(),'worker_pid':os.getpid()}
    def save():
        payload['updated_at']=c.now_iso(); c._write_json_atomic(path,payload)
        c._write_json_atomic(out/'status.json',{'updated_at':payload['updated_at'],'pid':os.getpid(),'account_node':node,
            'run_id':args.run_id,'active_phase':payload.get('active_phase'),'current_date':payload.get('current_date'),
            'current_progress':payload.get('current_progress'),'actual_new_posts':len(payload['posts']),
            'planned_days':len(plan['queue']),'attempted_days':len({r['slice_start'] for r in payload['search_runs']}),
            'verified_continuation_ends':sum(c.search_end_verified(r) for r in payload['search_runs']),
            'finalized':payload['finalized'],'guard_hold':payload.get('guard_hold'),
            'cooldown_until_epoch':payload.get('cooldown_until_epoch'),'original_data_written':False,
            'recovery_policy':payload.get('recovery_policy'),
            'prior_chain_verified':False,'natural_day_complete_proven':False,'platform_all_posts_proven':False})
    payload['active_phase']='starting'; save()
    try:
        with c.sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(session),headless=False,executable_path=str(c.CHROME),locale='en-US',viewport={'width':1400,'height':1000})
            try:
                if 'auth_token' not in {cookie['name'] for cookie in context.cookies()}:
                    raise RuntimeError('需要用户重新登录')
                page=context.pages[0] if context.pages else context.new_page()
                processed=0
                for item in plan['queue']:
                    if any(r.get('source_record_index')==item['source_record_index'] and r.get('source_path')==item['source_path'] and r.get('completed_at') for r in payload['search_runs']):
                        continue
                    # A newly written hold always wins over the plan's snapshot.
                    for receipt in plan['sources']:
                        now,sha=read(c.Path(receipt['path']))
                        if sha!=receipt['sha256'] or not manual_pause_verified(node,c.Path(receipt['path']),now):
                            raise RuntimeError('历史源变化/新增保护，停止独立续采')
                    if processed:
                        time.sleep(random.uniform(cfg['delay_min'],cfg['delay_max']))
                    if processed and processed%cfg['break_every']==0:
                        time.sleep(random.uniform(cfg['break_min'],cfg['break_max']))
                    day=item['slice_start']; seed=list(baseline[day].values())
                    seed.extend(p for p in payload['posts'] if local_day(p)==day)
                    record=next((r for r in reversed(payload['search_runs']) if r.get('source_record_index')==item['source_record_index'] and r.get('source_path')==item['source_path']),None)
                    if record is None:
                        record={**item,'started_at':c.now_iso(),'stop_reason':'in_progress','new_saved_posts':0,
                                'resumed_from_page':item['json_pages_used'],'baseline_saved_posts':len(seed),
                                'original_last_cursor':item['last_cursor'],'retry_history':[]}
                        payload['search_runs'].append(record)
                    payload.update(current_date=day,active_phase='searching',current_progress={'source_page':item['json_pages_used'],'page':record['json_pages_used'],'new_saved_posts':record['new_saved_posts']}); save()
                    print(f"[精确续采] {node.upper()} {item['keyword']} {day} 原尝试{item['source_attempt_count']}条/页{item['json_pages_used']}，基线合并{len(seed)}条；不从首页采集。",flush=True)
                    def checkpoint(rows,progress):
                        for post in rows:
                            key=str(post.get('post_id') or post.get('post_url') or '')
                            if key and key not in known:
                                known.add(key); payload['posts'].append(post); record['new_saved_posts']+=1
                        record.update(progress); record['checkpoint_at']=c.now_iso()
                        payload['current_progress']={'source_page':item['json_pages_used'],'page':progress['json_pages_used'],
                            'new_saved_posts':record['new_saved_posts'],'baseline_union_with_continuation':progress['count'],
                            'parsed_this_continuation':progress['parsed_post_count'],'date_rejected_this_continuation':progress['date_rejected_count']}
                        save(); print(f"[实际落盘] {day} 页{progress['json_pages_used']}，新增{record['new_saved_posts']}，累计独立新增{len(payload['posts'])}。",flush=True)
                    for attempt in range(cfg['retry_limit']+1):
                        try:
                            _,coverage=c.collect_search_graphql(page,item['keyword'],1,0,cfg['max_scrolls'],out,
                                date.fromisoformat(day),date.fromisoformat(day),search_type=cfg['search_type'],
                                no_growth_stop=cfg['no_growth_stop'],request_delay_min=cfg['graphql_delay_min'],request_delay_max=cfg['graphql_delay_max'],
                                timezone_offset=cfg['timezone_offset'],pagination_trigger_attempts=3,pagination_trigger_timeout_ms=15000,
                                checkpoint=checkpoint,resume_record=record,seed_rows=seed)
                            record.update(coverage); record['completed_at']=c.now_iso(); save()
                            if record.get('date_rejected_count'): raise RuntimeError('date_filter_mismatch；停止核验')
                            print(f"[续采结算] {day} {coverage['state_code']} 新增{record['new_saved_posts']}；只声明本次游标链末端，不补证旧页链。",flush=True)
                            break
                        except Exception as exc:
                            record['retry_history'].append({'at':c.now_iso(),'error':f'{type(exc).__name__}: {exc}',
                                'page':record.get('json_pages_used'),'has_cursor':bool(record.get('last_cursor'))})
                            save()
                            if may_verify_404(args.verify_404_once,exc,record,attempt,cfg['retry_limit']):
                                event={'at':c.now_iso(),'error':str(exc),'page':record['json_pages_used'],'verified':False}
                                record.setdefault('automatic_404_verifications',[]).append(event)
                                held={'at':c.now_iso(),'error':f'{type(exc).__name__}: {exc}','reason':'404_pending_bounded_verification'}
                                pause=backoff_seconds(exc,cfg)
                                payload.update(guard_hold=held,active_phase='404_verification_cooldown',cooldown_until_epoch=time.time()+pause);save()
                                print(f'[404受限复核] 保留保护，按原退避等待{pause/60:.1f}分钟；仅核验同查询/保存游标/保存页控制组，每日期一次。',flush=True)
                                time.sleep(pause)
                                payload['active_phase']='404_verifying';save()
                                def recovery_state(phase,until):
                                    payload.update(active_phase=phase,cooldown_until_epoch=until);save()
                                try:
                                    verified_rows,verification,proof_file=verify_transient_404(page,record,out,cfg,on_state=recovery_state)
                                except Exception as verification_error:
                                    event['verification_error']=str(verification_error);save();raise
                                event.update(verified=True,proof_file=proof_file,resolved_at=c.now_iso())
                                # Preserve the actual newly verified next page before proceeding.
                                durable_seed=list(baseline[day].values())+[p for p in payload['posts'] if local_day(p)==day]
                                merged={str(p.get('post_id') or p.get('post_url')):p for p in durable_seed+verified_rows}
                                checkpoint(list(merged.values()),{'query':record['query'],'slice_start':day,'slice_end':day,
                                    'count':len(merged),'json_pages_used':record['json_pages_used']+1,
                                    'last_cursor':verification['next_cursor'],'parsed_post_count':record.get('parsed_post_count',0)+len(verified_rows),
                                    'date_rejected_count':0,'stop_reason':'in_progress','coverage_status':'needs_retry'})
                                payload.setdefault('guard_hold_history',[]).append({**held,'resolved_at':c.now_iso(),
                                    'resolution':'explicitly_authorized_bounded_fresh_query_cursor_control_verification','proof_file':proof_file})
                                payload.update(guard_hold=None,active_phase='searching',cooldown_until_epoch=None)
                                seed=list(merged.values());save()
                                if not verification['next_cursor']:
                                    record.update(stop_reason='cursor_exhausted',coverage_status='apparently_exhausted',
                                        search_end_evidence={'policy':'response_end_v1','responses':[verification]},completed_at=c.now_iso())
                                    record.update(c.classify_search_run(record));save();break
                                if record['json_pages_used']>=cfg['max_scrolls']:
                                    record.update(stop_reason='max_pages',coverage_status='truncated',completed_at=c.now_iso())
                                    record.update(c.classify_search_run(record));save();break
                                continue
                            if fatal(exc): raise
                            pause=max(random.uniform(cfg['backoff_min'],cfg['backoff_max'])*(attempt+1),
                                      (getattr(exc,'retry_after',None) or getattr(exc,'search_retry_after',0) or 0)+random.uniform(10,45))
                            payload.update(active_phase='cooldown',cooldown_until_epoch=time.time()+pause); save()
                            print(f'[退避] {exc}；等待{pause/60:.1f}分钟，仅续最后已保存游标。',flush=True); time.sleep(pause)
                            if attempt>=cfg['retry_limit']:
                                record.update(stop_reason='interrupted_with_saved_sample',interruption_error=str(exc),completed_at=c.now_iso())
                                save(); break
                            seed=list(baseline[day].values())+[p for p in payload['posts'] if local_day(p)==day]
                            payload.update(active_phase='searching',cooldown_until_epoch=None)
                    processed+=1
                    # New instances can stop cooperatively at a persisted date
                    # boundary for future code rollouts; never close mid-page.
                    request=out/'reload_requested.json'
                    if request.exists():
                        control=json.loads(request.read_text(encoding='utf-8'))
                        if control.get('account_node')==node and control.get('run_id')==args.run_id:
                            payload['active_phase']='rollout_boundary_exit';save();break
                payload['current_date']=None
                payload['finalized']=all(any(r.get('source_path')==i['source_path'] and r.get('source_record_index')==i['source_record_index'] and c.search_end_verified(r) for r in payload['search_runs']) for i in plan['queue'])
                payload['active_phase']='continuation_ends_verified' if payload['finalized'] else 'continuation_pass_finished_needs_review'
            finally:
                context.close()
    except KeyboardInterrupt:
        payload['active_phase']='user_paused'; payload['finalized']=False
    except Exception as exc:
        payload.update(active_phase='guard_hold',finalized=False,
                       guard_hold={'at':c.now_iso(),'error':f'{type(exc).__name__}: {exc}','reason':'independent_continuation_requires_review'})
        print(f'[保护停止] {exc}',flush=True)
    finally:
        save(); lock.unlink(missing_ok=True)
        if payload['posts']:
            from x_export_column_txt import export_rows
            export_rows(payload['posts'],node.upper(),out/f'{node.upper()}_continuation_new_posts.txt')

if __name__=='__main__': main()
