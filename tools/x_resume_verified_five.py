"""Explicit user-authorized resume; default is read-only preflight, not launch."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

import x_keyword_collector as c
from x_continue_saved_cursors import account_busy
from x_plan_cursor_continuation import read,manual_pause_verified
from x_workbench.resume import validate_pause_resume


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause-dir',type=Path,required=True)
    parser.add_argument('--launch',action='store_true')
    args=parser.parse_args()
    pause=args.pause_dir.resolve();outputs=(c.ROOT/'outputs').resolve()
    if not pause.is_relative_to(outputs):raise ValueError('Pause report outside project outputs')
    receipt=json.loads((pause/'report.json').read_text(encoding='utf-8'))
    report={'at':c.now_iso(),'explicit_user_resume':bool(args.launch),'accounts':{},'automation_resumed':False}
    prepared=[]
    for node in 'abcde':
        try:
            folder=outputs/f'cursor_continuation_20261012/{node}/collection'
            path=folder/'continuation_raw.json';plan_path=folder.parent/'continuation_plan.json'
            if path.with_suffix('.sqlite3').exists():raise ValueError('Already has a database: review its current authoritative state, not an old pause snapshot')
            raw,sha=read(path);plan,plan_sha=read(plan_path)
            if plan.get('account_node')!=node:raise ValueError('Plan account mismatch')
            paused_sha=hashlib.sha256((pause/node/'after_continuation_raw.json').read_bytes()).hexdigest()
            item=receipt['accounts'][node]
            validate_pause_resume(raw,item,sha,paused_sha,plan_sha)
            session=c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if node=='a' else '-'+node))
            if account_busy(node,plan['run_id'],session):raise ValueError('Account has a collector or browser; no second worker')
            config_path=Path(plan['config_path']).resolve()
            if not config_path.is_relative_to(outputs):raise ValueError('Unexpected config location')
            cfg,cfg_sha=read(config_path)
            if cfg_sha!=plan['config_sha256'] or (cfg['search_engine'],cfg['author_strategy'],cfg['repair_policy'])!=('graphql','deferred','coverage_first'):
                raise ValueError('Original collection conditions drifted')
            for source in plan['sources']:
                source_path=Path(source['path']).resolve()
                if not source_path.is_relative_to(outputs):raise ValueError('Unexpected baseline source location')
                baseline,source_sha=read(source_path)
                if source_sha!=source['sha256'] or not manual_pause_verified(node,source_path,baseline):
                    raise ValueError('Historical source changed or has unresolved protection')
                del baseline
            lock=folder/'worker.lock'
            if lock.exists() and lock.read_text().strip()!=str(item['old_pid']):raise ValueError('Unexpected lock owner')
            if (folder/'reload_requested.json').exists():raise ValueError('Outstanding rollout request')
            report['accounts'][node]={'ready':True,'run_id':plan['run_id'],'saved_posts':len(raw['posts']),
                'date':raw.get('current_date'),'progress':raw.get('current_progress'),'planned_days':len(plan['queue']),
                'backend_on_resume':'sqlite','raw_sha_before':sha}
            prepared.append((node,folder,path,plan_path,plan,raw,lock,session))
        except Exception as exc:
            report['accounts'][node]={'ready':False,'blocked':str(exc)}
    if not args.launch:
        print(json.dumps(report,ensure_ascii=False,indent=2));return
    output=outputs/'resume_authoritative_20261015'/str(time.time_ns());output.mkdir(parents=True)
    for node,folder,path,plan_path,plan,raw,lock,session in prepared:
        if account_busy(node,plan['run_id'],session):
            report['accounts'][node].update(ready=False,blocked='Account became busy before launch');continue
        if (hashlib.sha256(path.read_bytes()).hexdigest()!=report['accounts'][node]['raw_sha_before']
                or hashlib.sha256(plan_path.read_bytes()).hexdigest()!=raw['plan_sha256']):
            report['accounts'][node].update(ready=False,blocked='Checkpoint/plan changed after preflight');continue
        backup=output/node;backup.mkdir()
        for name in ('continuation_raw.json','status.json','worker.lock','user_pause_receipt.json'):
            if (folder/name).exists():shutil.copy2(folder/name,backup/name)
        hold=raw.get('guard_hold')
        if hold:
            raw.setdefault('guard_hold_history',[]).append({**hold,'resolved_at':c.now_iso(),
                'resolution':'verified_user_pause_and_new_explicit_resume_authorization','pause_report':str(pause/'report.json')})
        raw.update(guard_hold=None,active_phase='user_authorized_resume_waiting_worker')
        raw.setdefault('manual_resume_history',[]).append({'at':c.now_iso(),'backup':str(backup),'posts_preserved':len(raw['posts']),
            'reason':'explicit_user_resume_after_authoritative_reader_acceptance'})
        c._write_json_atomic(path,raw)
        if lock.exists():lock.unlink()
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
        command=[str(c.ROOT/'.venv/Scripts/python.exe'),'-u','tools/x_continue_saved_cursors.py',
            '--account-node',node,'--run-id',plan['run_id'],'--plan',str(plan_path),'--search-engine','graphql',
            '--author-strategy','deferred','--pagination-trigger-attempts','3','--pagination-trigger-timeout-ms','15000',
            '--verify-404-once','--checkpoint-backend','sqlite']
        with (backup/'collector.log').open('wb') as stdout,(backup/'stderr.log').open('wb') as stderr:
            child=subprocess.Popen(command,cwd=c.ROOT,stdout=stdout,stderr=stderr,startupinfo=startup,creationflags=subprocess.CREATE_NEW_CONSOLE)
        report['accounts'][node].update(launcher_pid=child.pid,log=str(backup/'collector.log'),stderr=str(backup/'stderr.log'),backup=str(backup))
        c._write_json_atomic(output/'report.json',report)
    c._write_json_atomic(output/'report.json',report)
    print(json.dumps({'report':str(output/'report.json'),**report},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
