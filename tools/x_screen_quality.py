"""Evidence-based screening, never delete or relabel raw collection records."""
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from x_export_column_txt import RUNS, ROOT, export_rows

OUT=ROOT/'outputs/quality_iteration_20261008'
TECH={'pagination_not_triggered','no_network_response','interrupted_with_saved_sample','in_progress','max_pages','max_scrolls','date_filter_mismatch','target_reached'}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    totals=[]; days=[]
    for batch,run_id in RUNS.items():
        folder=ROOT/'outputs/x_keyword_runs'/run_id; path=folder/'raw.json'
        source=path.read_bytes(); before=hashlib.sha256(source).hexdigest()
        raw=json.loads(source.decode('utf-8-sig')); cfg=json.loads((folder/'run_config.json').read_text(encoding='utf-8-sig'))
        keyword=raw['requested_keywords'][0]; count=Counter(); hits=Counter(); invalid=0; seen=set(); duplicates=0
        for p in raw['posts']:
            key=p.get('post_id') or p.get('post_url')
            duplicates+=bool(key and key in seen)
            if key: seen.add(key)
            try:
                stamp=datetime.fromisoformat(p['posted_at'].replace('Z','+00:00'))
                if stamp.tzinfo is None: raise ValueError('no timezone')
                day=stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat()
                count[day]+=1
                hits[day]+=keyword.casefold() in str(p.get('post_text','')).casefold()
            except (ValueError,TypeError,KeyError): invalid+=1
        def matches(p): return keyword.casefold() in str(p.get('post_text','')).casefold()
        export_rows((p for p in raw['posts'] if matches(p)),batch,OUT/f'{batch}_{keyword}_body_hit.txt')
        export_rows((p for p in raw['posts'] if not matches(p)),batch,OUT/f'{batch}_{keyword}_body_not_hit_review.txt')
        grouped={}
        for r in raw.get('search_runs',[]): grouped.setdefault((r.get('slice_start'),r.get('slice_end')),[]).append(r)
        dt=datetime(2026,1,1)
        while dt<=datetime(2026,9,30):
            day=dt.date().isoformat(); records=grouped.get((day,day),[]); last=records[-1] if records else {}; reason=last.get('stop_reason','')
            in_cfg=cfg['start_date']<=day<=cfg['end_date']
            if not in_cfg: category='outside_original_config'; priority=0
            elif not records: category='unvisited'; priority=1
            elif not count[day] and reason!='cursor_exhausted': category='zero_unverified'; priority=1
            elif reason in TECH: category='technical_or_unsettled'; priority=2
            elif reason in {'no_new_results','repeated_cursor'}: category='heuristic_stop_not_full_proof'; priority=3
            elif reason=='cursor_exhausted': category='interface_cursor_exhausted_not_platform_full'; priority=4
            else: category='unknown_stop'; priority=2
            days.append({'batch':batch,'keyword':keyword,'date':day,'in_original_config':in_cfg,'saved_posts':count[day],
                'body_hit':hits[day],'body_not_hit':count[day]-hits[day],'last_reason':reason,'category':category,'priority':priority,
                'latest_pages':last.get('json_pages_used',''),'error':last.get('interruption_error') or last.get('error',''),
                'history_has_cursor_exhausted':any(r.get('stop_reason')=='cursor_exhausted' for r in records),
                'action':'report_scope_gap_no_auto_change' if not in_cfg else 'pending_verified_supplement' if priority<=2 else 'retain_and_review'})
            dt+=timedelta(days=1)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before
        summary={'batch':batch,'keyword':keyword,'posts':len(raw['posts']),'body_hit':sum(hits.values()),'body_not_hit':len(raw['posts'])-sum(hits.values()),'duplicate_rows_on_id_or_url':duplicates,'invalid_dates':invalid,'raw_sha256':before,'raw_unchanged':True}
        totals.append(summary); print(json.dumps(summary,ensure_ascii=False),flush=True)
    days.sort(key=lambda r:(r['priority'],r['saved_posts'],r['batch'],r['date']))
    with (OUT/'daily_quality_and_priority.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(days[0])); w.writeheader(); w.writerows(days)
    summary={'batches':totals,'day_categories':dict(Counter(r['category'] for r in days)),'collection_started':False,'deletions':0,'literal_rule':'casefold substring; GPT includes ChatGPT; not semantic relevance'}
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    report=['# 第一轮质量筛查：2026-10-08','', '原数据未删除、未修改。正文命中只按忽略大小写子串判断，不是语义标注，不认定未命中就是无关。',
        '', '每批 body_hit.txt 是正文核验命中集，body_not_hit_review.txt 是正文未命中待复核集。二者合计等于该批原始总数。日期与原文列布局与全量TXT相同。',
        '', 'daily_quality_and_priority.csv 按零条/未访问、技术中断、启发式停止、接口游标耗尽排序，同类按实际保存量升序；低量只是排序，不是硬判漏采，不自动扩大原日期范围。',
        '', '旧游标和旧查询必须验证关联；有历史cursor_exhausted的日期需先复核，不能仅因为最后一条记录异常便重跑。',
        '', '尚未开始批量补采或恢复心跳。分页改动的本地测试通过不代表真实X页面验证通过。', '', '|批次|总数|正文命中|正文未命中待核验|', '|---|---:|---:|---:|']
    report += [f"|{s['batch']} {s['keyword']}|{s['posts']}|{s['body_hit']}|{s['body_not_hit']}|" for s in totals]
    report+=['', '日期分类计数（含原配置外日期）：',json.dumps(summary['day_categories'],ensure_ascii=False)]
    (OUT/'REPORT.md').write_text('\n'.join(report),encoding='utf-8')

if __name__=='__main__': main()
