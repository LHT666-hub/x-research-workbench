"""Bounded A probe of a saved empty-tail cursor using fresh browser-created requests."""
import argparse
import hashlib
import json
import time
from datetime import date
import x_keyword_collector as c


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account-node',choices=['a','b'],default='a')
    parser.add_argument('--control-before-tail',action='store_true')
    args=parser.parse_args()
    node=args.account_node
    out = c.ROOT/('outputs/terminal_cursor_probe_20261012' + ('_b' if node=='b' else '') + ('_control' if args.control_before_tail else ''))
    out.mkdir(parents=True,exist_ok=True)
    if (out/'status.json').exists():
        raise RuntimeError('Probe already exists; do not overwrite evidence')
    source = c.ROOT/('outputs/reproducibility_a_gpt_20261009_20261011_retry3/round_1' if node=='a' else 'outputs/reproducibility_b_gpt_20261009_20261011_retry2/round_1')
    manifest = json.loads((source/'responses_manifest.json').read_text(encoding='utf-8'))
    last = manifest[-1]
    tail = json.loads((source/last['response_file']).read_text(encoding='utf-8'))
    tail_rows,cursor = c.parse_search_timeline(tail,'gpt',1)
    if tail_rows or not cursor:
        raise RuntimeError('Source must be a zero-post response with a cursor')
    expected_control_ids=[]
    if args.control_before_tail:
        cursor=manifest[-3]['next_cursor']
        expected_control_ids=manifest[-2]['parsed_post_ids']
    state={'started_at':c.now_iso(),'account':node,'state':'preflight','source':str(source),'source_sha256':hashlib.sha256((source/last['response_file']).read_bytes()).hexdigest(),'source_empty':True,'probes':[],'platform_all_posts_proven':False}
    state['control_before_tail']=args.control_before_tail
    def save():
        state['updated_at']=c.now_iso(); c._write_json_atomic(out/'status.json',state)
    save()
    query=c.build_search_query('gpt',date(2026,6,25),date(2026,6,25),8)
    url='https://x.com/search?q='+c.quote(query)+'&src=typed_query&f=live'
    try:
        with c.sync_playwright() as pw:
            profile='tmp/chrome-x-keyword-profile'+('-b' if node=='b' else '')
            context=pw.chromium.launch_persistent_context(str(c.ROOT/profile),headless=False,executable_path=str(c.CHROME),locale='en-US',viewport={'width':1400,'height':1000})
            try:
                if 'auth_token' not in {cookie['name'] for cookie in context.cookies()}:
                    raise RuntimeError('A requires login')
                page=context.pages[0] if context.pages else context.new_page()
                with page.expect_response(lambda r:c._search_response_matches(r,query,''),timeout=60000) as pending:
                    page.goto(url,wait_until='domcontentloaded',timeout=60000)
                initial=pending.value
                preflight=c.checked_search_response(initial)
                c._write_json_atomic(out/'preflight_response.json',preflight)
                rows,_=c.parse_search_timeline(preflight,'gpt',1)
                if not rows or any(not c.post_in_date_range(r['posted_at'],date(2026,6,25),date(2026,6,25),8) for r in rows):
                    raise RuntimeError('Current A health/date validation failed')
                rate=c._rate_limit_state(c._response_headers(initial))
                state.update(state='probing_saved_cursor',preflight_http=initial.status,preflight_parsed=len(rows)); save()
                for number in range(1,4):
                    if rate.get('remaining',999)<=5:
                        while time.time()<rate.get('reset',0): time.sleep(min(15,rate['reset']-time.time()))
                    time.sleep(4)
                    requested_cursor=cursor
                    redirected=[]
                    # X creates a new signed first-page request on each reload.
                    # Change only its query cursor; retain fresh browser headers,
                    # same query/path/account and all server reset protections.
                    def route_handler(route):
                        request=route.request
                        if not redirected and c._search_raw_query(request.url,request.post_data)==query and c._search_request_cursor(request.url,request.post_data)=='':
                            redirected.append(True)
                            route.continue_(url=c._search_url_with_cursor(request.url,requested_cursor))
                        else:
                            route.continue_()
                    page.route('**/*SearchTimeline*',route_handler)
                    try:
                        with page.expect_response(lambda r:c._search_response_matches(r,query,requested_cursor),timeout=60000) as pending:
                            page.reload(wait_until='domcontentloaded',timeout=60000)
                        response=pending.value
                        metadata={'probe':number,'at':c.now_iso(),'http_status':response.status,'browser_request_redirected':bool(redirected),'actual_request_cursor_matches':c._search_request_cursor(response.request.url,response.request.post_data)==requested_cursor}
                        state['probes'].append(metadata); save()
                        payload=c.checked_search_response(response)
                        c._write_json_atomic(out/f'probe_{number}_response.json',payload)
                        posts,next_cursor=c.parse_search_timeline(payload,'gpt',1)
                        c._write_json_atomic(out/f'probe_{number}_posts.json',posts)
                        metadata.update(parsed_count=len(posts),date_rejected_count=sum(not c.post_in_date_range(r['posted_at'],date(2026,6,25),date(2026,6,25),8) for r in posts),has_next_cursor=bool(next_cursor),cursor_repeated=next_cursor==requested_cursor)
                        if args.control_before_tail:
                            metadata['matches_archived_last_nonempty_ids']=set(r['post_id'] for r in posts)==set(expected_control_ids)
                        rate=c._rate_limit_state(c._response_headers(response)); metadata['rate_limit']=rate; save()
                        if posts or not next_cursor:
                            break
                        cursor=next_cursor
                    finally:
                        page.unroute('**/*SearchTimeline*',route_handler)
                state.update(state='bounded_probe_finished',all_valid_probes_empty=bool(state['probes']) and all(p.get('http_status')==200 and p.get('parsed_count')==0 and p.get('actual_request_cursor_matches') for p in state['probes']))
            finally:
                context.close()
    except Exception as exc:
        state.update(state='probe_blocked_no_automatic_retry',error=f'{type(exc).__name__}: {exc}',guard_hold={'at':c.now_iso(),'error':str(exc)})
    finally:
        state['finished_at']=c.now_iso(); save(); print(json.dumps(state,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
