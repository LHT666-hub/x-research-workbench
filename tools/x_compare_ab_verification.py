"""One-shot local evidence audit; no network, no collector restart."""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import x_keyword_collector as c


def compare(left, right):
    union = left | right
    common = left & right
    return {'left_count':len(left), 'right_count':len(right), 'intersection':len(common), 'union':len(union), 'jaccard':len(common)/len(union) if union else 1.0, 'only_left':sorted(left-right), 'only_right':sorted(right-left), 'identical':left==right}


def main():
    out = c.ROOT/'outputs/ab_completeness_20261009'
    out.mkdir(parents=True,exist_ok=True)
    while True:
        states = {}
        for node in ('a','b'):
            path = c.ROOT/f'outputs/reproducibility_{node}_gpt_20261009/status.json'
            if path.exists():
                states[node] = json.loads(path.read_text(encoding='utf-8'))
        c._write_json_atomic(out/'status.json', {'state':'waiting_two_account_experiments','updated_at':c.now_iso(),'account_states':{k:v.get('state') for k,v in states.items()}})
        if len(states)==2 and all(s.get('finished_at') for s in states.values()):
            break
        time.sleep(15)
    results, sets = {}, {}
    for node in ('a','b'):
        for number in (1,2):
            key=f'{node}{number}'
            directory = c.ROOT/f'outputs/reproducibility_{node}_gpt_20261009/round_{number}'
            result_path=directory/'result.json'
            if not result_path.exists():
                results[key]={'state':'round_not_completed','error':states[node].get('error')}
                continue
            data=json.loads(result_path.read_text(encoding='utf-8'))
            manifest=json.loads((directory/'responses_manifest.json').read_text(encoding='utf-8'))
            ids={str(p['post_id']) for p in data['posts']}
            sets[key]=ids
            verified_hashes=all(hashlib.sha256((directory/r['response_file']).read_bytes()).hexdigest()==r['payload_sha256'] for r in manifest)
            chain=all(manifest[i]['request_cursor']==manifest[i-1]['next_cursor'] for i in range(1,len(manifest)))
            terminal=json.loads((directory/manifest[-1]['response_file']).read_text(encoding='utf-8')) if manifest else {}
            terminal_rows, terminal_cursor=c.parse_search_timeline(terminal,'gpt',1)
            instructions=[]
            def visit(item):
                if isinstance(item,dict):
                    kind=item.get('type','')
                    if isinstance(kind,str) and 'Terminate' in kind:
                        instructions.append({k:item[k] for k in ('type','direction') if k in item})
                    for value in item.values():visit(value)
                elif isinstance(item,list):
                    for value in item:visit(value)
            visit(terminal)
            invalid=[]
            for post in data['posts']:
                stamp=datetime.fromisoformat(str(post['posted_at']).replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8)))
                if stamp.date().isoformat()!='2026-06-25':invalid.append(post['post_id'])
            reason=data['coverage']['stop_reason']
            results[key]={'post_count':len(ids),'stop_reason':reason,'pages':data['coverage'].get('json_pages_used'),'all_payload_hashes_valid':verified_hashes,'request_cursor_chain_valid':chain,'last_http':manifest[-1]['http_status'] if manifest else None,'last_payload_post_count':len(terminal_rows),'last_payload_has_next_cursor':bool(terminal_cursor),'termination_instructions':instructions,'out_of_date_ids':invalid,'body_literal_gpt_count':sum('gpt' in p['post_text'].casefold() for p in data['posts']),'response_end_verified':reason=='cursor_exhausted' and bool(manifest) and not terminal_cursor and verified_hashes and chain and not invalid}
    comparisons={}
    for l,r in [('a1','a2'),('b1','b2'),('a1','b1'),('a2','b2')]:
        if l in sets and r in sets: comparisons[f'{l}_vs_{r}']=compare(sets[l],sets[r])
    report={'finished_at':c.now_iso(),'query':'gpt since_time:1782316800 until_time:1782403200','date_timezone':'Asia/Shanghai','rounds':results,'comparisons':comparisons,'platform_all_posts_proven':False,'all_four_search_endings_verified':len(results)==4 and all(r.get('response_end_verified') for r in results.values()),'batch_collection_restart_permitted':False}
    c._write_json_atomic(out/'report.json',report)
    lines=['# A/B gpt 单日分页与重复性验证','',f'生成时间：{report["finished_at"]}。目标2026-06-25，北京时间，Latest。','', '|轮次|帖子数|页数|停止原因|末页有下一游标|响应结束已核验|','|---|---:|---:|---|---|---|']
    for key,r in results.items():lines.append(f'|{key}|{r.get("post_count", "—")}|{r.get("pages", "—")}|{r.get("stop_reason",r.get("state"))}|{r.get("last_payload_has_next_cursor", "—")}|{r.get("response_end_verified", False)}|')
    lines += ['', '## ID集合对比', '', '|对比|交集|并集|Jaccard|ID完全一致|','|---|---:|---:|---:|---|']
    for key,r in comparisons.items():lines.append(f'|{key}|{r["intersection"]}|{r["union"]}|{r["jaccard"]:.6f}|{r["identical"]}|')
    lines += ['', '## 结论边界', '', '成功响应无后续游标且原始证据核验通过，仅记该次搜索分页结束。技术/启发式/上限结束一律未确认结束。同账号两轮一致仅说明本实验观察到的重复稳定性，跨账号差异须报告，不能外推全平台覆盖。帖子正文命中另列，不混同日期准确性。', '', '没有平台总量真值，本实验不能计算真实召回率。日查询与小时分片结果并集一致性尚未测试，不宣称通过。要宣称“本条件下相对完整”，至少须搜索结束证据、同条件复采稳定性及分片一致性分别有记录，且公开不确定性；不以40条或任意数量阈值认定完整。', '', '定时守护保持关闭；所有批量任务保持停止，验证结束也不恢复。完整差集、原始证据校验结果和末页终止指令见report.json及各轮响应。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    c._write_json_atomic(out/'status.json',{'state':'local_evidence_report_finished','updated_at':c.now_iso(),'report':'REPORT.md','platform_all_posts_proven':False})


if __name__ == '__main__':
    main()
