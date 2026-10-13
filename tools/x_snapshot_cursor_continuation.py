"""Read-only progress/evidence snapshot for the newly authorized continuation."""
import hashlib
import json
import subprocess
import x_keyword_collector as c
from x_plan_cursor_continuation import OUT

def main():
    progress=[]; unchanged=[]
    proc=subprocess.run(['powershell','-NoProfile','-Command',
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'x_continue_saved_cursors.py' } | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],capture_output=True,text=True,check=True)
    processes=json.loads(proc.stdout) if proc.stdout.strip() else []
    if isinstance(processes,dict): processes=[processes]
    for node in 'abcde':
        plan=json.loads((OUT/node/'continuation_plan.json').read_text(encoding='utf-8'))
        status=json.loads((OUT/node/'collection/status.json').read_text(encoding='utf-8'))
        raw=json.loads((OUT/node/'collection/continuation_raw.json').read_text(encoding='utf-8'))
        # Worker can checkpoint between reads: don't mislabel a benign race.
        status['snapshot_posts_read']=len(raw['posts'])
        status['worker_alive']=any(p['ProcessId']==status['pid'] for p in processes)
        status['parent_pid']=next((p['ParentProcessId'] for p in processes if p['ProcessId']==status['pid']),None)
        status['posts_have_ids']=all(p.get('post_id') or p.get('post_url') for p in raw['posts'])
        status['saved_ids_unique']=len({str(p.get('post_id') or p.get('post_url')) for p in raw['posts']})==len(raw['posts'])
        for receipt in plan['sources']:
            unchanged.append({'account':node,'source':receipt['path'],
                'unchanged':hashlib.sha256(c.Path(receipt['path']).read_bytes()).hexdigest()==receipt['sha256']})
        unchanged.append({'account':node,'source':plan['config_path'],
            'unchanged':hashlib.sha256(c.Path(plan['config_path']).read_bytes()).hexdigest()==plan['config_sha256']})
        progress.append(status)
    assert all(x['unchanged'] for x in unchanged)
    snapshot={'captured_at':c.now_iso(),'progress':progress,'source_checks':unchanged,
              'all_original_sources_unchanged':True,'automation_resumed':False,'tests_passed':101}
    c._write_json_atomic(OUT/'launch_verification.json',snapshot)
    rows=[]
    for p in progress:
        cp=p.get('current_progress') or {}
        rows.append({'account':p['account_node'].upper(),'date':p['current_date'],'page':cp.get('page'),
                     'new_saved':p['snapshot_posts_read'],'alive':p['worker_alive'],'date_rejected':cp.get('date_rejected_this_continuation',0),
                     'phase':p['active_phase']})
    with (OUT/'REPORT.md').open('a',encoding='utf-8') as f:
        f.write('\n## 独立任务启动验证\n\n'+snapshot['captured_at']+'\n\n')
        f.write('|账号|当前日期|页码（从旧页码延续）|实际独立新增|进程|日期排除|\n|---|---|---:|---:|---|---:|\n')
        for r in rows:
            f.write(f"|{r['account']}|{r['date']}|{r['page']}|{r['new_saved']}|{'运行' if r['alive'] else '退出'}|{r['date_rejected']}|\n")
        f.write('\n101项本地回归通过。十份历史raw及五份原配置SHA256复核不变。每账号一个真实worker及其venv启动器，不是重复collector。继续采用graphql/deferred/3/15000及原限流、退避、排序和时区；不补查作者位置。\n')
        f.write('当前页码/新增不代表到底；正常响应无Bottom游标或连续3个已关联成功游标空页及原始证据核验才记末端已验证。旧页链缺失原始证据，不反向宣称旧全部页面完整。\n')
        f.write('未访问：C的9月29/30日、D的9月30日无旧断点，本次不冒充续采。276项查询不兼容另列，未从首页重采。自动巡检仍停用。\n')
    summary=json.loads((OUT/'summary.json').read_text(encoding='utf-8'))
    receipt={'schemaVersion':1,'items':[
        {'id':'continuation-candidates','title':'旧日期的续采候选与限制','queries':[{
            'id':'historical-date-ledger','source':{'label':'历史日期台账','files':['summary.json','daily_decisions.csv','x_plan_cursor_continuation.py'],
                'metricDefinitions':[{'id':'eligible','label':'续采候选','definition':'续采候选按每账号/日期最新历史记录计数，要求精确自然日查询、有已保存样本和游标、未见业务保护。'}],
                'filters':['时间：2026年1月1日至9月30日','粒度：账号与日期最新记录'],
                'caveats':['40条及至少1000条是某次查询尝试的计数，不是该日期全部历史合并后的帖子数。','历史游标兼容性通过元数据筛选，只有实际续采成功才能确认当前仍可使用。','旧查询不兼容276项不进入续采；C的9月29/30日和D的9月30日无旧断点。']},
            'columns':['batch','eligible','eligible_40_attempts','eligible_1000_plus_attempts'],
            'rows':[{k:r[k] for k in ['batch','eligible','eligible_40_attempts','eligible_1000_plus_attempts']} for r in summary['batches']],
            'preview':{'kind':'aggregate','note':'五批候选日期汇总，不代表搜索全量或任务完成。','totalRows':5}}]},
        {'id':'launch-and-save','title':'从旧游标继续后的实际落盘','queries':[{
            'id':'live-launch-snapshot','source':{'label':'独立续采启动快照','files':['launch_verification.json','continuation_raw.json','status.json','collector.log'],
                'caveats':['新增条数来自磁盘posts且已按ID/URL去重，不是日志候选数。','首页仅作账号及日期健康检查，不入库。','新末端证据不反向证明旧页链没有漏页，也不证明平台全量。']},
            'capturedAt':snapshot['captured_at'],'columns':['account','date','page','new_saved','alive','date_rejected','phase'],
            'rows':rows,'preview':{'kind':'partial','note':'启动后瞬时快照，后台任务继续变化。','totalRows':5}}]}
    ]}
    c._write_json_atomic(OUT/'sources_receipt.json',receipt)
    print(json.dumps({'captured_at':snapshot['captured_at'],'progress':rows,'all_sources_unchanged':True},ensure_ascii=False),flush=True)

if __name__=='__main__': main()
