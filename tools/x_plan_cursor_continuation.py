"""Read-only historical audit; produce exact-query continuation plans, not repairs."""
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import x_keyword_collector as c
from x_export_column_txt import RUNS

OUT=c.ROOT/'outputs/cursor_continuation_20261012'

def read(path):
    blob=path.read_bytes()
    return json.loads(blob.decode('utf-8-sig')),hashlib.sha256(blob).hexdigest()

def local_day(post):
    try:
        return datetime.fromisoformat(post['posted_at'].replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).date().isoformat()
    except (KeyError,ValueError,TypeError):
        return ''

def manual_pause_verified(node,path,raw):
    hold=raw.get('guard_hold')
    if not hold: return True
    expected='BrowserContext.close: Connection closed while reading from the driver'
    if hold.get('error')!=expected: return False
    if node!='b':
        report=json.loads((c.ROOT/'outputs/ab_verification_stop_20261009/report.json').read_text(encoding='utf-8'))[node]
        return (report.get('ctrl_c_sent') and report.get('all_pre_stop_ids_preserved')
                and report.get('guard_hold')==hold and len(raw['posts'])==report['saved_after'])
    backup=c.ROOT/'outputs/reproducibility_b_gpt_20261009/b_pause_backup/supplement_raw.json'
    prior,_=read(backup)
    return (hold.get('detected_at')=='2026-10-09T07:42:53+08:00'
            and raw['posts']==prior['posts'] and len(raw['posts'])==17980)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    receipt=[]; ledger=[]
    for label,run_id in RUNS.items():
        node=label.lower(); folder=c.ROOT/'outputs/x_keyword_runs'/run_id
        original,original_sha=read(folder/'raw.json')
        cfg,cfg_sha=read(folder/'run_config.json')
        assert cfg['search_engine']=='graphql' and cfg['author_strategy']=='deferred' and cfg['repair_policy']=='coverage_first'
        supplemental=(c.ROOT/'outputs/quality_iteration_20261008/d_supplement/supplement_raw.json' if node=='d'
                      else c.ROOT/f'outputs/staged_collection_20261008/{node}/collection/supplement_raw.json')
        sources=[(folder/'raw.json',original,original_sha)]
        if supplemental.exists():
            raw,sha=read(supplemental); sources.append((supplemental,raw,sha))
        source_receipts=[{'path':str(p.resolve()),'sha256':sha,'rows':len(raw['posts']),
                          'guard_hold':raw.get('guard_hold'),'manual_pause_verified':manual_pause_verified(node,p,raw) if p==supplemental else not bool(raw.get('guard_hold'))}
                         for p,raw,sha in sources]
        # All old posts are retained for ID/URL de-duplication, independent of
        # which search attempt's cursor is selected. No old query equivalence assumed.
        date_rows=defaultdict(dict)
        original_counts=Counter(local_day(p) for p in original['posts'])
        latest={}
        for path,raw,sha in sources:
            for post in raw['posts']:
                key=str(post.get('post_id') or post.get('post_url') or '')
                if key: date_rows[local_day(post)][key]=post
            for index,record in enumerate(raw.get('search_runs',[])):
                day=record.get('slice_start','')
                if day and day==record.get('slice_end') and '2026-01-01'<=day<='2026-09-30':
                    stamp=record.get('checkpoint_at') or record.get('completed_at') or record.get('started_at') or ''
                    if day not in latest or stamp>=latest[day][0]: latest[day]=(stamp,path,sha,index,record)
        blocked=any(not x['manual_pause_verified'] for x in source_receipts)
        queue=[]; counts=Counter()
        for day in sorted(latest):
            _,path,sha,index,r=latest[day]
            classification=c.classify_search_run(r)
            keyword=r.get('keyword',''); query=r.get('query','')
            expected=c.build_search_query(keyword,date.fromisoformat(day),date.fromisoformat(day),cfg['timezone_offset'])
            pages=int(r.get('json_pages_used',0) or 0); count=int(r.get('count',0) or 0)
            reason=r.get('stop_reason','')
            decision=('verified_end_skip' if classification['search_end_verified'] else
                      'incompatible_date_query' if query!=expected else
                      'date_quality_hold' if classification['state_category']=='quality_hold' else
                      'record_protection_hold' if classification['state_category']=='protection_hold' else
                      'account_guard_blocked' if blocked else
                      'no_saved_cursor' if not r.get('last_cursor') else
                      'no_valid_saved_sample' if not date_rows[day] or count<=0 else
                      'page_budget_or_record_invalid' if not 0<pages<cfg['max_scrolls'] else
                      'eligible_cursor_continuation')
            item={'account_node':node,'run_id':run_id,'keyword':keyword,'slice_start':day,'slice_end':day,
                  'source_path':str(path.resolve()),'source_sha256':sha,'source_record_index':index,
                  'query':query,'search_type':cfg['search_type'],'source_attempt_count':count,
                  'original_saved_date_count':original_counts[day],'baseline_union_date_count':len(date_rows[day]),
                  'json_pages_used':pages,'last_cursor':r.get('last_cursor',''),
                  'date_rejected_count':r.get('date_rejected_count',0),'original_stop_reason':reason,
                  'source_state_code':classification['state_code'],'decision':decision,
                  'priority':'40_post_attempt' if count==40 else 'large_unverified_attempt' if count>=1000 else 'other_unverified_attempt',
                  'prior_chain_verified':False}
            counts[decision]+=1
            ledger.append({k:v for k,v in item.items() if k!='last_cursor'})
            if decision=='eligible_cursor_continuation':
                queue.append(item)
        queue.sort(key=lambda r:(0 if r['source_attempt_count']==40 else 1 if r['source_attempt_count']>=1000 else 2,
                                 -r['source_attempt_count'] if r['source_attempt_count']>=1000 else 0,r['slice_start']))
        plan={'created_at':c.now_iso(),'account_node':node,'run_id':run_id,'mode':'exact_cursor_continuation_no_first_page_fallback',
              'sources':source_receipts,'config_path':str((folder/'run_config.json').resolve()),'config_sha256':cfg_sha,
              'queue':queue,'decision_counts':dict(counts),'manual_guards_preserved':True,
              'search_engine':'graphql','author_strategy':'deferred','pagination_trigger_attempts':3,
              'pagination_trigger_timeout_ms':15000,'repair_policy':'coverage_first','platform_all_posts_proven':False}
        (OUT/node).mkdir(parents=True,exist_ok=True)
        c._write_json_atomic(OUT/node/'continuation_plan.json',plan)
        result={'batch':label,'keyword':original['requested_keywords'][0],'original_posts':len(original['posts']),
                'date_records':len(latest),'eligible':len(queue),'eligible_40_attempts':sum(i['source_attempt_count']==40 for i in queue),
                'eligible_1000_plus_attempts':sum(i['source_attempt_count']>=1000 for i in queue),
                'decision_counts':dict(counts),'unvisited_dates':[ (date(2026,1,1)+timedelta(days=i)).isoformat() for i in range(273) if (date(2026,1,1)+timedelta(days=i)).isoformat() not in latest],
                'first_candidate':{k:v for k,v in queue[0].items() if k!='last_cursor'} if queue else None}
        receipt.append(result); print(json.dumps(result,ensure_ascii=False),flush=True)
    c._write_json_atomic(OUT/'summary.json',{'created_at':c.now_iso(),'batches':receipt,'raw_files_unchanged':True,'automation_resumed':False})
    with (OUT/'daily_decisions.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(ledger[0])); writer.writeheader(); writer.writerows(ledger)
    lines=['# 历史游标续采审计','',f'生成：{c.now_iso()}','',
           '一行是每账号/日期最新历史检查点。原数据与人工保护标记不改写；仅查询完全相同且有样本/游标、无业务保护的日期进入独立续采。',
           '40条是某次查询计数，不一定等于该日期合并后的实际帖子数。已保存页100将请求其下一页游标，而非从首页重采。',
           '旧查询不兼容、缺失游标、超预算均待另行处理。旧页链未保留原始响应，续采核验尾部不能反向证明旧100页从未遗漏。',
           '', '|批次|可续采日|40条尝试|≥1000条尝试|查询不兼容|缺失游标|','|---|---:|---:|---:|---:|---:|']
    for r in receipt:
        lines.append(f"|{r['batch']}|{r['eligible']}|{r['eligible_40_attempts']}|{r['eligible_1000_plus_attempts']}|{r['decision_counts'].get('incompatible_date_query',0)}|{r['decision_counts'].get('no_saved_cursor',0)}|")
    (OUT/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

if __name__=='__main__': main()
