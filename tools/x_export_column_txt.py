"""Export immutable raw snapshots to tab-column TXT; leave collector state untouched."""
import csv
import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNS={'A':'20260928_122030','B':'20260928_233433_b','C':'20260929_001443_c','D':'20260929_002436_d','E':'20260929_105257_e'}
OUT=ROOT/'outputs/exports_txt_20261008'
HEADER=['批次','关键词','北京时间','帖子ID','正文','帖子链接','作者账号','作者名称','正文含关键词_忽略大小写','已有自填位置','已有Account_based_in','采集时间']
TECHNICAL={'pagination_not_triggered','no_network_response','interrupted_with_saved_sample','in_progress','max_pages','max_scrolls','date_filter_mismatch','target_reached'}

def escape(value):
    return str('' if value is None else value).replace('\\','\\\\').replace('\t','\\t').replace('\r','\\r').replace('\n','\\n').replace('\x00','\\0')

def export_rows(posts,batch,destination):
    with destination.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f,delimiter='\t',quoting=csv.QUOTE_NONE,escapechar=None,lineterminator='\n')
        writer.writerow(HEADER)
        for p in posts:
            try:
                stamp=datetime.fromisoformat(p.get('posted_at','').replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).isoformat()
            except (ValueError,TypeError): stamp=p.get('posted_at','')
            keyword=str(p.get('keyword',''))
            vals=[batch,keyword,stamp,p.get('post_id',''),p.get('post_text',''),p.get('post_url',''),p.get('author_username',''),p.get('author_display_name',''),bool(keyword and keyword.casefold() in str(p.get('post_text','')).casefold()),p.get('profile_location',''),p.get('account_based_in',''),p.get('collected_at','')]
            # Quotes are ordinary text in TSV: write escaped columns directly.
            f.write('\t'.join(escape(v) for v in vals)+'\n')


def export_checkpoint(source, destination, batch, *, snapshot=None):
    """Export exactly one authoritative revision, never a stale JSON snapshot."""
    from x_workbench.state import read_state
    from x_workbench.storage import write_json_atomic
    snapshot = snapshot if snapshot is not None else read_state(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=destination.name+'.',suffix='.tmp',dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        export_rows(snapshot.state['posts'],batch,temporary)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {**snapshot.receipt(), 'rows':snapshot.post_count,'columns':len(HEADER),
               'export_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
               'scope':'this_checkpoint_only_not_merged_historical_total'}
    write_json_atomic(destination.with_suffix('.receipt.json'),receipt)
    return receipt

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=[]; plan=[]
    for batch,run_id in RUNS.items():
        folder=ROOT/'outputs/x_keyword_runs'/run_id
        source=folder/'raw.json'; data=source.read_bytes(); before=hashlib.sha256(data).hexdigest()
        from x_workbench.state import read_state
        snapshot=read_state(source);raw=snapshot.state
        config=json.loads((folder/'run_config.json').read_text(encoding='utf-8-sig'))
        keyword=raw['requested_keywords'][0]; dest=OUT/f'{batch}_{keyword}_posts.txt'
        export_receipt=export_checkpoint(source,dest,batch,snapshot=snapshot)
        with dest.open(encoding='utf-8-sig') as f:
            assert next(f).rstrip('\n').split('\t')==HEADER
            count=0
            for line in f:
                assert len(line.rstrip('\n').split('\t'))==len(HEADER)
                count+=1
        assert count==len(raw['posts'])
        assert hashlib.sha256(source.read_bytes()).hexdigest()==before,'Source changed during export'
        latest={}
        for r in raw.get('search_runs',[]): latest[(r.get('slice_start'),r.get('slice_end'))]=r
        for (start,end),r in sorted(latest.items()):
            reason=r.get('stop_reason','')
            if reason in TECHNICAL or (reason=='no_new_results' and not r.get('count')):
                plan.append({'batch':batch,'run_id':run_id,'account_node':config.get('account_node'), 'keyword':keyword,'date_start':start,'date_end':end,'latest_reason':reason,'pages':r.get('json_pages_used'),'attempt_posts':r.get('count'),'error':r.get('interruption_error') or r.get('error',''),'status':'pending_live_pagination_validation_and_sufficiency_definition'})
        manifest.append({'batch':batch,'keyword':keyword,'file':dest.name,'rows':count,'columns':len(HEADER),'bytes':dest.stat().st_size,'source_sha256':before,'source_snapshot':snapshot.receipt(),'export_sha256':export_receipt['export_sha256'],'raw_unchanged':True})
        print(json.dumps(manifest[-1],ensure_ascii=False),flush=True)
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'exception_repair_plan.json').write_text(json.dumps({'collector_started':False,'original_configs_unchanged':True,'needs_sufficiency_definition':True,'repair_policy':'coverage_first','outside_original_config':'A/B/C September 29-30 and D/E September 30 are not added to collection automatically','queue':plan},ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'README.txt').write_text('五批独立TXT导出，UTF-8 BOM，制表符分栏，一条帖子一行。\n可在Excel选择“从文本/CSV”，编码UTF-8，分隔符制表符，帖子ID列指定为文本（避免长ID丢精度）。\n正文的反斜杠、换行、制表符分别转义为\\\\、\\n、\\r、\\t；原始正文仍在raw.json。\n正文含关键词字段只是忽略大小写的子串核验，不是语义相关性；GPT会匹配ChatGPT。未删除任何帖子，也没有补查作者位置。\n全部行数及输入输出SHA256见manifest.json。旧CSV/ZIP未覆盖。\nexception_repair_plan.json是待验证的异常清单，不是已执行补采，不证明每天完整或足够。\n',encoding='utf-8-sig')
    print(f'异常候选日期 {len(plan)}；未启动补采',flush=True)

if __name__=='__main__': main()
