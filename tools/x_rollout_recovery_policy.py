"""One-time rollout at durable date boundaries. Not a heartbeat or monitor.

Never changes original batches/configs/queues, starts a second account worker,
or terminates Chrome/unrelated Python. Old dedicated worker consoles can be
paused only during a verified persisted date boundary. A stalled shutdown is
cleaned only after its browser and Playwright driver have already exited.
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
import x_keyword_collector as c
from x_export_column_txt import RUNS
from x_continue_saved_cursors import account_busy
from x_recovery_policy import POLICY_ID,protected_error,backoff_seconds,enabled_by_settings

OUT=c.ROOT/'outputs/recovery_policy_rollout_20261012'

def cim():
    result=subprocess.run(['powershell','-NoProfile','-Command',
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"],capture_output=True,text=True,check=True)
    data=json.loads(result.stdout);return data if isinstance(data,list) else [data]

def identity(p,node):
    command=p.get('CommandLine') or ''
    return (p['Name'].lower().startswith('python') and 'x_continue_saved_cursors.py' in command
            and f'--account-node {node}' in command and f'--run-id {RUNS[node.upper()]}' in command)

def safe_boundary(raw,status,cfg,now):
    if raw.get('guard_hold') or not raw.get('search_runs') or raw.get('finalized'):return False
    record=raw['search_runs'][-1]
    if not record.get('completed_at') or raw.get('current_date')!=record.get('slice_start'):return False
    if status.get('current_date')!=record.get('slice_start') or status.get('active_phase')!='searching':return False
    try:age=(now-datetime.fromisoformat(record['completed_at'])).total_seconds()
    except (ValueError,TypeError):return False
    # The unchanged original inter-date wait is at least 15 seconds. Leave a
    # generous margin and re-read just before the console signal.
    return 0<=age<min(8,cfg['delay_min']/2)

def session(node):
    return c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if node=='a' else '-'+node))

def spawn_worker(node):
    folder=c.ROOT/f'outputs/cursor_continuation_20261012/{node}/collection'
    stamp=str(time.time_ns());stdout=(folder/f'policy_v2_{stamp}.log').open('wb');stderr=(folder/f'policy_v2_{stamp}.stderr.log').open('wb')
    startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
    executable=c.ROOT/'.venv/Scripts/python.exe'
    command=[str(executable),'-u','tools/x_continue_saved_cursors.py','--account-node',node,'--run-id',RUNS[node.upper()],
        '--plan',f'outputs/cursor_continuation_20261012/{node}/continuation_plan.json','--search-engine','graphql',
        '--author-strategy','deferred','--pagination-trigger-attempts','3','--pagination-trigger-timeout-ms','15000','--verify-404-once']
    try:child=subprocess.Popen(command,cwd=c.ROOT,stdout=stdout,stderr=stderr,startupinfo=startup,creationflags=subprocess.CREATE_NEW_CONSOLE)
    finally:stdout.close();stderr.close()
    return child.pid

def clear_stale_lock(folder,old_pid,backup):
    lock=folder/'worker.lock'
    if lock.exists():
        marker=lock.read_text().strip()
        if marker!=str(old_pid):raise RuntimeError('锁不属于刚核验退出的worker，不清理')
        c._write_json_atomic(backup/'retired_lock.json',{'at':c.now_iso(),'owner_pid':old_pid})
        lock.unlink()

def run_hidden_script(script,args,folder,tag):
    stdout=(folder/(tag+'.log')).open('wb');stderr=(folder/(tag+'.stderr.log')).open('wb')
    startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
    try:child=subprocess.Popen([str(c.ROOT/'.venv/Scripts/python.exe'),'-u',script,*args],cwd=c.ROOT,
        stdout=stdout,stderr=stderr,startupinfo=startup,creationflags=subprocess.CREATE_NEW_CONSOLE)
    finally:stdout.close();stderr.close()
    return child

def recover_stopped_404(node,raw,status,cfg,folder):
    if raw['guard_hold'].get('error')!='RuntimeError: SearchTimeline HTTP 404':
        raise RuntimeError('非404业务保护，不自动清除')
    if raw['search_runs'][-1].get('automatic_404_verifications'):
        raise RuntimeError('当前日期自动复核次数已用完，不重复放行')
    if account_busy(node,RUNS[node.upper()],session(node)):
        raise RuntimeError('保护退出后仍有账号进程/浏览器，不能重复启动')
    backup=OUT/node/('stopped_404_'+str(time.time_ns()));backup.mkdir(parents=True)
    for attempt in range(cfg['retry_limit']+1):
        probe=backup/f'probe_{attempt}'
        child=run_hidden_script('tools/x_probe_continuation_404.py',
            ['--account-node',node,'--output',str(probe)],backup,f'probe_{attempt}')
        # This is a background, one-time transition job; normal reset waits
        # happen inside the probe without blocking the user's chat turn.
        code=child.wait(timeout=1800)
        if code or not (probe/'status.json').exists():raise RuntimeError('现场核验进程失败，保留保护')
        proof=json.loads((probe/'status.json').read_text(encoding='utf-8'))
        if proof.get('state')=='404_recovery_verified':
            child=run_hidden_script('tools/x_apply_404_recovery.py',
                ['--account-node',node,'--proof',str(probe/'status.json'),'--automatic-policy'],backup,'apply')
            if child.wait(timeout=120):raise RuntimeError('现场证据未通过独立放行检查')
            clear_stale_lock(folder,status['pid'],backup)
            return {'phase':'starting_v2','offline_404_recovered':True,'proof_file':str(probe/'status.json'),
                    'new_launcher_pid':spawn_worker(node),'backup':str(backup)}
        error=RuntimeError(proof.get('error') or '现场复核失败')
        if protected_error(error) or attempt>=cfg['retry_limit']:raise error
        # The latest probe records authoritative headers on a 429; all other
        # transient failures still use the original bounded backoff.
        samples=proof.get('samples',[]);rate=(samples[-1].get('rate_limit') or {}) if samples else {}
        explicit=max(0,rate.get('reset',0)-time.time()) if rate.get('remaining',999)<=5 else 0
        error.search_retry_after=explicit
        time.sleep(backoff_seconds(error,cfg,attempt))

def switch_at_boundary(node,raw,status,cfg,folder):
    processes=cim();pid=status['pid'];worker=next((p for p in processes if p['ProcessId']==pid),None)
    if not worker or not identity(worker,node):raise RuntimeError('worker身份改变，不操作')
    launcher=worker['ParentProcessId'];parent=next((p for p in processes if p['ProcessId']==launcher),None)
    if not parent or not identity(parent,node):raise RuntimeError('专用启动器身份改变，不操作')
    actors={launcher,pid}
    actors.update(p['ProcessId'] for p in processes if p['ParentProcessId']==pid and p['Name'].lower()=='node.exe'
                  and 'playwright' in (p.get('CommandLine') or '').lower())
    backup=OUT/node/str(time.time_ns());backup.mkdir(parents=True)
    c._write_json_atomic(backup/'before_raw.json',raw);c._write_json_atomic(backup/'before_status.json',status)
    latest=json.loads((folder/'continuation_raw.json').read_text(encoding='utf-8'))
    latest_status=json.loads((folder/'status.json').read_text(encoding='utf-8'))
    if not safe_boundary(latest,latest_status,cfg,datetime.now().astimezone()) or latest_status['pid']!=pid:
        return {'phase':'waiting_date_boundary','backup':str(backup)}
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.FreeConsole()
    if not kernel.AttachConsole(pid):raise RuntimeError('不能连接账号专用console，不强行终止')
    kernel.SetConsoleCtrlHandler(None,True)
    buffer=(ctypes.c_uint32*128)();count=kernel.GetConsoleProcessList(buffer,128);members=set(list(buffer)[:count])
    if count>128 or not members.issubset(actors|{os.getpid()}):
        kernel.FreeConsole();raise RuntimeError('Console有不属于该账号的进程，不发送信号')
    sent=bool(kernel.GenerateConsoleCtrlEvent(0,0));kernel.FreeConsole()
    if not sent:raise RuntimeError('边界暂停信号未发送')
    report={'node':node,'at':c.now_iso(),'phase':'boundary_signal_sent','only_owned_console':True,
        'console_pids':sorted(members),'source_day':latest['search_runs'][-1]['slice_start'],'saved_posts':len(latest['posts'])}
    c._write_json_atomic(backup/'transition.json',report)
    for tick in range(35):
        time.sleep(1);current=cim()
        remaining=[p for p in current if p['ProcessId'] in {pid,launcher} and identity(p,node)]
        chrome=[p for p in current if p['Name'].lower()=='chrome.exe' and '--type=' not in (p.get('CommandLine') or '')
                and str(session(node)) in (p.get('CommandLine') or '')
                and (node!='a' or not any(f'chrome-x-keyword-profile-{x}' in (p.get('CommandLine') or '') for x in 'bcde'))]
        driver=[p for p in current if p['Name'].lower()=='node.exe' and p['ParentProcessId']==pid]
        if not remaining and not chrome and not driver:break
        if tick>=8 and remaining and not chrome and not driver:
            # Only a shutdown remnant of the verified, checkpointed console.
            # Recheck each exact PID/command in the same PowerShell operation.
            for p in remaining:
                target=p['ProcessId']
                command=f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={target}'; if ($p -and $p.CommandLine -match 'x_continue_saved_cursors.py --account-node {node} --run-id {RUNS[node.upper()]}') {{ Stop-Process -Id {target} -Force }}"
                subprocess.run(['powershell','-NoProfile','-Command',command],check=True,capture_output=True)
            report['only_browserless_driverless_shutdown_remnants_cleaned']=True
    else:raise RuntimeError('账号仍有浏览器/采集进程；不修改状态或重复启动')
    if account_busy(node,RUNS[node.upper()],session(node)):raise RuntimeError('账号仍忙；不恢复')
    after=json.loads((folder/'continuation_raw.json').read_text(encoding='utf-8'))
    hold=after.get('guard_hold')
    if hold and hold.get('error')!='BrowserContext.close: Connection closed while reading from the driver':
        raise RuntimeError('出现新的业务保护，不清除')
    old_ids={str(p.get('post_id') or p.get('post_url')) for p in latest['posts']}
    new_ids={str(p.get('post_id') or p.get('post_url')) for p in after['posts']}
    if not old_ids.issubset(new_ids) or len(new_ids)!=len(after['posts']):raise RuntimeError('边界样本保留检查失败')
    c._write_json_atomic(backup/'after_stop_raw.json',after)
    if hold:after.setdefault('guard_hold_history',[]).append({**hold,'resolved_at':c.now_iso(),
        'resolution':'verified_policy_rollout_at_saved_date_boundary','transition_file':str(backup/'transition.json')})
    after.setdefault('policy_rollout_history',[]).append({'at':c.now_iso(),'policy_id':POLICY_ID,'backup':str(backup),'all_old_ids_preserved':True})
    after.update(guard_hold=None,active_phase='policy_rollout_waiting_worker')
    c._write_json_atomic(folder/'continuation_raw.json',after)
    clear_stale_lock(folder,pid,backup)
    report.update(phase='starting_v2',all_old_ids_preserved=True,new_launcher_pid=spawn_worker(node),backup=str(backup))
    c._write_json_atomic(backup/'transition.json',report)
    return report

def main():
    OUT.mkdir(exist_ok=True,parents=True);lock=OUT/'rollout.lock'
    handle=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.write(handle,str(os.getpid()).encode());os.close(handle)
    state={'started_at':c.now_iso(),'pid':os.getpid(),'policy_id':POLICY_ID,'one_time_not_automation':True,
           'accounts':{n:{'phase':'waiting_date_boundary'} for n in 'abcde'}}
    deadline=time.time()+3*3600
    try:
        while time.time()<deadline:
            if (OUT/'cancel_requested.json').exists():
                state['cancelled_at']=c.now_iso();c._write_json_atomic(OUT/'status.json',state);return
            pending=False
            for node,entry in state['accounts'].items():
                if entry['phase'] in {'active_v2','finished_no_restart','needs_manual_attention'}:continue
                if not enabled_by_settings(c.ROOT/'recovery_policy.json',node):
                    entry.update(phase='needs_manual_attention',error='授权配置已关闭该节点，不切换');continue
                pending=True;folder=c.ROOT/f'outputs/cursor_continuation_20261012/{node}/collection'
                raw=json.loads((folder/'continuation_raw.json').read_text(encoding='utf-8'))
                status=json.loads((folder/'status.json').read_text(encoding='utf-8'))
                receipt=status.get('recovery_policy') or {}
                if receipt.get('id')==POLICY_ID and receipt.get('enabled'):
                    process=next((p for p in cim() if p['ProcessId']==status['pid']),None)
                    if (process and identity(process,node)
                        and receipt.get('worker_pid')==status['pid']):
                        # A live v2 worker may hold its own temporary 404 guard
                        # while legally cooling down and verifying. Deployment
                        # is confirmed, not collector health or task completion;
                        # never start an offline probe against that live account.
                        entry.update(phase='active_v2',worker_pid=status['pid'],activated_at=c.now_iso(),
                                     collector_phase=status.get('active_phase'),
                                     guard_hold=status.get('guard_hold'));continue
                cfg=json.loads(c.Path(c.ROOT/f'outputs/x_keyword_runs/{RUNS[node.upper()]}/run_config.json').read_text(encoding='utf-8-sig'))
                if raw.get('guard_hold'):
                    if raw['guard_hold'].get('error')=='RuntimeError: SearchTimeline HTTP 404':
                        entry.update(phase='offline_404_verification',updated_at=c.now_iso())
                        c._write_json_atomic(OUT/'status.json',state)
                        try:entry.update(recover_stopped_404(node,raw,status,cfg,folder))
                        except Exception as exc:entry.update(phase='needs_manual_attention',error=str(exc),guard_hold=raw['guard_hold'])
                    else:entry.update(phase='needs_manual_attention',guard_hold=raw['guard_hold'])
                    continue
                if raw.get('finalized'):
                    entry.update(phase='finished_no_restart',future_policy_enabled=True);continue
                if entry['phase']=='starting_v2':
                    entry.setdefault('startup_wait_epoch',time.time())
                    if time.time()-entry['startup_wait_epoch']>90:
                        entry.update(phase='needs_manual_attention',error='新实例未确认激活；保留数据，不重复启动')
                    continue
                if safe_boundary(raw,status,cfg,datetime.now().astimezone()):
                    plan=json.loads((folder.parent/'continuation_plan.json').read_text(encoding='utf-8'))
                    if hashlib.sha256(c.Path(plan['config_path']).read_bytes()).hexdigest()!=plan['config_sha256']:
                        entry.update(phase='needs_manual_attention',error='原配置变化；不切换正常进程');continue
                    try:entry.update(switch_at_boundary(node,raw,status,cfg,folder))
                    except Exception as exc:entry.update(phase='needs_manual_attention',error=str(exc))
            state['updated_at']=c.now_iso();c._write_json_atomic(OUT/'status.json',state)
            if not pending:break
            time.sleep(0.7)
        for entry in state['accounts'].values():
            if entry['phase'] not in {'active_v2','finished_no_restart','needs_manual_attention'}:
                entry.update(phase='needs_manual_attention',error='3小时内未取得安全切换边界；未强停正常任务')
        state['finished_at']=c.now_iso();c._write_json_atomic(OUT/'status.json',state)
    finally:lock.unlink(missing_ok=True)
    print(json.dumps(state,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
