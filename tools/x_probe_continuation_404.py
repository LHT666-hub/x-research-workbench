"""Bounded, fresh-browser verification of an authorized protected saved cursor.

Does not edit collector state or clear a guard. No author requests.
"""
import argparse
import hashlib
import json
import random
import time
from datetime import date
import x_keyword_collector as c
from x_continue_saved_cursors import account_busy

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account-node',choices=list('abcde'),required=True)
    parser.add_argument('--output',type=c.Path,required=True)
    args=parser.parse_args(); node=args.account_node; out=args.output
    out.mkdir(parents=True,exist_ok=False)
    folder=c.ROOT/f'outputs/cursor_continuation_20261012/{node}/collection'
    source=folder/'continuation_raw.json'; blob=source.read_bytes(); raw=json.loads(blob)
    assert raw.get('guard_hold',{}).get('error')=='RuntimeError: SearchTimeline HTTP 404'
    record=raw['search_runs'][-1]; day=date.fromisoformat(record['slice_start'])
    query=c.build_search_query(record['keyword'],day,day,8)
    assert query==record['query'] and record.get('last_cursor') and record['json_pages_used']<1000
    session=c.ROOT/'tmp'/('chrome-x-keyword-profile'+('' if node=='a' else '-'+node))
    if account_busy(node,raw['original_run_id'],session): raise RuntimeError('账号忙；不启动第二个任务')
    anchors=[]
    for path in folder.glob('search_end_evidence/*/responses_manifest.json'):
        records=json.loads(path.read_text(encoding='utf-8'))
        if records and records[-1]['query']==query and records[-1]['page']==record['json_pages_used'] and records[-1]['next_cursor']==record['last_cursor']:
            anchors.append(records[-1])
    if len(anchors)!=1: raise RuntimeError('无法唯一匹配保存页的原始响应与游标')
    anchor=anchors[0]; anchor_blob=c.Path(anchor['response_file']).read_bytes()
    assert hashlib.sha256(anchor_blob).hexdigest()==anchor['sha256']
    state={'started_at':c.now_iso(),'account_node':node,'source_path':str(source.resolve()),
           'source_sha256':hashlib.sha256(blob).hexdigest(),'held_guard':raw['guard_hold'],
           'query':query,'day':day.isoformat(),'keyword':record['keyword'],'search_type':'live',
           'from_page':record['json_pages_used'],'saved_cursor':record['last_cursor'],
           'state':'preflight','events':[],'samples':[],'guard_cleared':False}
    def save():
        state['updated_at']=c.now_iso(); c._write_json_atomic(out/'status.json',state)
    def snapshot(response,name,requested):
        headers=c._response_headers(response)
        item={'at':c.now_iso(),'http_status':response.status,'query':c._search_raw_query(response.url,response.request.post_data),
              'request_cursor':c._search_request_cursor(response.url,response.request.post_data),
              'request_cursor_matches':c._search_request_cursor(response.url,response.request.post_data)==requested,
              'endpoint_path':c._graphql_operation(response.url)[1],'rate_limit':c._rate_limit_state(headers)}
        state['samples'].append(item); save()
        payload=c.checked_search_response(response,out/'failures',query,requested)
        file=out/f'{name}_response.json'; c._write_json_atomic(file,payload)
        posts,cursor=c.parse_search_timeline(payload,record['keyword'],1)
        item.update(response_file=str(file.resolve()),sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
                    parsed_count=len(posts),next_cursor=cursor or '',
                    date_rejected_count=sum(not c.post_in_date_range(p['posted_at'],day,day,8) for p in posts))
        c._write_json_atomic(out/f'{name}_posts.json',posts); save()
        if item['query']!=query or not item['request_cursor_matches'] or item['date_rejected_count']:
            raise RuntimeError('验证查询、日期或请求游标不一致')
        return item,posts
    def wait_rate(item):
        rate=item['rate_limit']; delay=random.uniform(2,4)
        if rate.get('remaining',999)<=5 and rate.get('reset',0)>time.time():
            delay=max(delay,rate['reset']-time.time()+random.uniform(5,20))
        state['state']='respecting_reset' if delay>4 else 'request_spacing'
        state['not_before_epoch']=time.time()+delay; save()
        print(f'[请求节奏] 等待{delay:.1f}秒，保留服务器reset。',flush=True)
        time.sleep(delay)
    save()
    try:
        with c.sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(session),headless=False,executable_path=str(c.CHROME),locale='en-US',viewport={'width':1400,'height':1000})
            try:
                if 'auth_token' not in {cookie['name'] for cookie in context.cookies()}:
                    raise RuntimeError('需要用户重新登录')
                page=context.pages[0] if context.pages else context.new_page()
                def event(response):
                    if c._search_raw_query(response.url,response.request.post_data)==query and 'SearchTimeline' in response.url:
                        state['events'].append({'at':c.now_iso(),'status':response.status,
                            'endpoint_path':c._graphql_operation(response.url)[1],
                            'request_cursor_matches_saved':c._search_request_cursor(response.url,response.request.post_data)==record['last_cursor']})
                        save()
                page.on('response',event)
                url='https://x.com/search?q='+c.quote(query)+'&src=typed_query&f=live'
                with page.expect_response(lambda r:c._search_response_matches(r,query,''),timeout=60000) as pending:
                    page.goto(url,wait_until='domcontentloaded',timeout=60000)
                preflight,first=snapshot(pending.value,'preflight','')
                if not first: raise RuntimeError('首页健康检查没有有效帖子')
                wait_rate(preflight)
                continuation,rows=snapshot(c._fresh_browser_cursor_response(page,query,record['last_cursor']), 'continuation',record['last_cursor'])
                if not rows or not continuation['next_cursor']:
                    raise RuntimeError('当前断点未返回有效继续页；不解除保护或把空页判到底')
                continuation['page']=record['json_pages_used']+1
                state['continuation_posts_file']=str((out/'continuation_posts.json').resolve()); save()
                wait_rate(continuation)
                control,control_rows=snapshot(c._fresh_browser_cursor_response(page,query,anchor['request_cursor']), 'control',anchor['request_cursor'])
                state['control_anchor_matches']=set(p['post_id'] for p in control_rows)==set(anchor['parsed_post_ids'])
                if not state['control_anchor_matches']:
                    raise RuntimeError('保存页控制组ID不一致；先复核，禁止自动放行')
                state.update(state='404_recovery_verified',preflight=preflight,continuation=continuation,control=control,
                             final_rate_limit=control['rate_limit'],original_posts_unchanged=source.read_bytes()==blob)
                assert state['original_posts_unchanged']
                print(f"[核验通过] 首页HTTP200，断点页{continuation['page']} HTTP200/{len(rows)}条，保存页控制组ID一致。",flush=True)
            finally:
                context.close()
    except Exception as exc:
        state.update(state='probe_blocked_guard_preserved',error=f'{type(exc).__name__}: {exc}')
        print(f'[核验未通过] {exc}',flush=True)
    finally:
        state['finished_at']=c.now_iso(); save()

if __name__=='__main__':main()
