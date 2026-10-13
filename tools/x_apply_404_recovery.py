"""Apply an explicitly authorized, independently verified account 404 recovery."""
import argparse
import hashlib
import json
from datetime import date,datetime
import x_keyword_collector as c
from x_continue_saved_cursors import account_busy
from x_recovery_policy import POLICY_ID

def verify(proof_path,source):
    proof=json.loads(proof_path.read_text(encoding='utf-8'))
    blob=source.read_bytes(); raw=json.loads(blob)
    hold=raw.get('guard_hold')
    if not hold or hold.get('error')!='RuntimeError: SearchTimeline HTTP 404' or hold!=proof.get('held_guard'):
        raise ValueError('不是本次已核验的404保护，禁止清除')
    if proof.get('state')!='404_recovery_verified' or proof.get('account_node') not in set('abcde') or raw.get('account_node')!=proof.get('account_node'):
        raise ValueError('核验状态/账号不符')
    if c.Path(proof['source_path']).resolve()!=source.resolve() or hashlib.sha256(blob).hexdigest()!=proof['source_sha256']:
        raise ValueError('源文件已变化，不沿用旧核验证据')
    age=(datetime.now().astimezone()-datetime.fromisoformat(proof['finished_at'])).total_seconds()
    if not 0<=age<=900: raise ValueError('现场核验超过15分钟，须重新核验')
    record=raw['search_runs'][-1]; day=date.fromisoformat(record['slice_start'])
    if (record['query']!=proof['query'] or record['json_pages_used']!=proof['from_page']
        or record['last_cursor']!=proof['saved_cursor'] or record.get('date_rejected_count',0)):
        raise ValueError('当前断点与证据不符')
    if proof['query']!=c.build_search_query(record['keyword'],day,day,8) or proof.get('search_type')!='live':
        raise ValueError('查询/排序改变')
    decoded={}
    for name in ('preflight','continuation','control'):
        evidence=proof[name]; body=c.Path(evidence['response_file']).read_bytes()
        if (evidence['http_status']!=200 or evidence['query']!=record['query'] or not evidence['request_cursor_matches']
            or evidence.get('date_rejected_count')!=0 or hashlib.sha256(body).hexdigest()!=evidence['sha256']):
            raise ValueError('HTTP/查询/游标/日期/响应哈希核验失败')
        payload=json.loads(body); c.validate_search_payload(payload)
        rows,cursor=c.parse_search_timeline(payload,record['keyword'],1)
        if (not rows or len(rows)!=evidence['parsed_count'] or cursor!=evidence['next_cursor']
            or any(not c.post_in_date_range(p['posted_at'],day,day,8) for p in rows)):
            raise ValueError('原始响应解析不符合核验记录')
        decoded[name]=rows
    continuation=proof['continuation']
    if continuation['request_cursor']!=record['last_cursor'] or continuation['page']!=record['json_pages_used']+1:
        raise ValueError('不是保存断点的下一页')
    if not continuation['next_cursor'] or proof['preflight']['request_cursor']!='':
        raise ValueError('首页或继续页结构不符')
    anchors=[]
    for path in source.parent.glob('search_end_evidence/*/responses_manifest.json'):
        records=json.loads(path.read_text(encoding='utf-8'))
        if records and records[-1]['query']==record['query'] and records[-1]['page']==record['json_pages_used'] and records[-1]['next_cursor']==record['last_cursor']:
            anchors.append(records[-1])
    if len(anchors)!=1: raise ValueError('保存页锚点不唯一')
    anchor=anchors[0]; body=c.Path(anchor['response_file']).read_bytes()
    if hashlib.sha256(body).hexdigest()!=anchor['sha256']: raise ValueError('保存页原始响应已变化')
    anchor_rows,_=c.parse_search_timeline(json.loads(body),record['keyword'],1)
    if (proof['control']['request_cursor']!=anchor['request_cursor']
        or {r['post_id'] for r in decoded['control']}!={r['post_id'] for r in anchor_rows}):
        raise ValueError('保存页控制组帖子ID不一致')
    # Do not take a separate posts JSON on trust: always parse the raw response.
    return raw,proof,decoded['continuation'],blob

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--proof',type=c.Path,required=True)
    parser.add_argument('--account-node',choices=list('abcde'),default='a')
    parser.add_argument('--automatic-policy',action='store_true')
    args=parser.parse_args()
    node=args.account_node
    source=c.ROOT/f'outputs/cursor_continuation_20261012/{node}/collection/continuation_raw.json'
    raw,proof,rows,before=verify(args.proof,source)
    if args.automatic_policy and raw['search_runs'][-1].get('automatic_404_verifications'):
        raise ValueError('本日期自动404复核次数已经用完，不重复放行')
    session=c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if node=='a' else '-'+node))
    if raw['account_node']!=node or account_busy(node,raw['original_run_id'],session):
        raise RuntimeError('账号不符或仍有worker/核验/浏览器；禁止修改运行中的数据')
    backup=args.proof.parent/'before_recovery'; backup.mkdir(exist_ok=False)
    c._write_json_atomic(backup/'continuation_raw.json',raw)
    c._write_json_atomic(backup/'status.json',json.loads((source.parent/'status.json').read_text(encoding='utf-8')))
    known={str(p.get('post_id') or p.get('post_url')) for p in raw['posts']}
    for receipt in raw['sources']:
        blob=c.Path(receipt['path']).read_bytes()
        if hashlib.sha256(blob).hexdigest()!=receipt['sha256']:raise RuntimeError('历史原始数据改变')
        known.update(str(p.get('post_id') or p.get('post_url')) for p in json.loads(blob)['posts'])
    record=raw['search_runs'][-1]; added=0
    if args.automatic_policy:
        record.setdefault('automatic_404_verifications',[]).append({'at':c.now_iso(),'policy_id':POLICY_ID,
            'verified':True,'source':'authorized_one_time_policy_rollout','proof_file':str(args.proof.resolve()),
            'page':record['json_pages_used']})
    for row in rows:
        key=str(row.get('post_id') or row.get('post_url'))
        if key and key not in known:
            known.add(key); added+=1; row['rank']=record['count']+added; raw['posts'].append(row)
    old_ids={str(p.get('post_id') or p.get('post_url')) for p in json.loads(before)['posts']}
    assert old_ids.issubset(known)
    raw.setdefault('guard_hold_history',[]).append({**raw['guard_hold'],'resolved_at':c.now_iso(),
        'resolution':'user_authorized_fresh_query_and_exact_cursor_control_verified',
        'proof_file':str(args.proof.resolve()),'backup_path':str(backup.resolve())})
    record.update(json_pages_used=proof['continuation']['page'],last_cursor=proof['continuation']['next_cursor'],
                  count=record['count']+added,new_saved_posts=record['new_saved_posts']+added,
                  parsed_post_count=record.get('parsed_post_count',0)+len(rows),checkpoint_at=c.now_iso(),
                  recovery_evidence_file=str(args.proof.resolve()))
    raw.update(guard_hold=None,active_phase='verified_404_recovered_waiting_worker',updated_at=c.now_iso())
    assert source.read_bytes()==before,'A源文件在恢复前发生变化'
    c._write_json_atomic(source,raw)
    c._write_json_atomic(args.proof.parent/'recovery_applied.json',{'at':c.now_iso(),'before_sha256':proof['source_sha256'],
        'after_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'all_pre_recovery_ids_preserved':True,
        'merged_probe_new_posts':added,'resumed_after_page':record['json_pages_used'],
        'guard_history_preserved':True,'original_batches_not_written':True})
    print(json.dumps({'guard_resolved':True,'posts_preserved':len(old_ids),'probe_added':added,
                      'next_page':record['json_pages_used']+1,'new_total':len(raw['posts'])},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
