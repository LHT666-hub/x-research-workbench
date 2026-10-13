"""One-shot B experiment: fresh day search versus two half-day partitions."""
import argparse
import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
import x_keyword_collector as c


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-node', choices=['a','b'], default='b')
    parser.add_argument('--halves-only', action='store_true')
    args = parser.parse_args()
    node = args.account_node
    out = c.ROOT / (f'outputs/search_halves_20261012/{node}' if args.halves_only else 'outputs/search_bottom_20261012' + ('_a' if node == 'a' else ''))
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'status.json').exists():
        raise RuntimeError('Experiment already exists; do not overwrite')
    previous = c.ROOT / ('outputs/reproducibility_a_gpt_20261009_20261011_retry3/status.json' if node == 'a' else 'outputs/reproducibility_b_gpt_20261009_20261011_retry2/status.json')
    baseline_dir = previous.parent
    if args.halves_only:
        previous = c.ROOT / ('outputs/terminal_cursor_probe_20261012' + ('_b' if node == 'b' else '') + '_control/status.json')
    state = {'account_node': node, 'started_at': c.now_iso(), 'state': 'waiting_existing_account_to_exit', 'segments': []}
    def save():
        state['updated_at'] = c.now_iso()
        c._write_json_atomic(out / 'status.json', state)
    save()
    original_builder, original_check, original_filter = c.build_search_query, c.checked_search_response, c.post_in_date_range
    original_next = c._next_browser_signed_search_response
    try:
        absent_checks = 0
        while True:
            old = json.loads(previous.read_text(encoding='utf-8'))
            if old.get('guard_hold'):
                raise RuntimeError('Existing B experiment guard_hold; no recovery')
            browser_condition = "-match 'chrome-x-keyword-profile' -and $_.CommandLine -notmatch 'chrome-x-keyword-profile-[bcde]'" if node == 'a' else "-match 'chrome-x-keyword-profile-b'"
            worker_condition = "-match 'account-node a'" if node == 'a' else "-notmatch 'account-node a'"
            command = "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -and $_.ProcessId -notin @(" + str(__import__('os').getpid()) + ',' + str(__import__('os').getppid()) + ") -and $_.CommandLine -match 'x_verify_b_gpt.py|x_run_quality_supplement.py|x_verify_search_bottom.py|x_probe_terminal_cursor.py' -and $_.CommandLine " + worker_condition + ") -or ($_.Name -eq 'chrome.exe' -and $_.CommandLine " + browser_condition + ") } | Select-Object -ExpandProperty ProcessId"
            processes = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, text=True, check=True)
            if not processes.stdout.strip():
                absent_checks += 1
                if old.get('finished_at') or absent_checks >= 3:
                    state['previous_experiment_state'] = old.get('state')
                    state['previous_exit_reason'] = old.get('error') or ('recorded_finish' if old.get('finished_at') else 'process_absent_with_unfinished_checkpoint_unknown_exit')
                    save()
                    break
            else:
                absent_checks = 0
            time.sleep(15)
        cfg = json.loads((c.ROOT / 'outputs/x_keyword_runs/20260928_233433_b/run_config.json').read_text(encoding='utf-8-sig'))
        if args.halves_only:
            health = json.loads(previous.read_text(encoding='utf-8'))
            if health.get('state') != 'bounded_probe_finished' or not health.get('probes') or health['probes'][-1].get('http_status') != 200:
                raise RuntimeError('Current account diagnostic not verified; no recovery')
            rate = health['probes'][-1].get('rate_limit', {})
            if rate.get('remaining',999)<=5:
                while time.time()<rate.get('reset',0): time.sleep(min(15,rate['reset']-time.time()))
        # Respect the most recent response's authoritative reset before reusing B.
        old_dir = previous.parent
        for number in (2, 1):
            manifest_path = old_dir / f'round_{number}/responses_manifest.json'
            if manifest_path.exists():
                last = json.loads(manifest_path.read_text(encoding='utf-8'))[-1]
                deadline = float(last.get('rate_limit', {}).get('reset') or 0)
                while time.time() < deadline:
                    state['state'] = 'waiting_previous_reset'; save()
                    time.sleep(min(15, deadline - time.time()))
                break
        with c.sync_playwright() as pw:
            profile = 'tmp/chrome-x-keyword-profile' + ('-b' if node == 'b' else '')
            context = pw.chromium.launch_persistent_context(str(c.ROOT / profile), headless=False, executable_path=str(c.CHROME), locale='en-US', viewport={'width': 1400, 'height': 1000})
            try:
                if 'auth_token' not in {x['name'] for x in context.cookies()}:
                    raise RuntimeError('B requires login')
                page = context.pages[0] if context.pages else context.new_page()
                sets = {}
                segment_plan = [('day',1782316800,1782403200), ('first_half',1782316800,1782360000), ('second_half',1782360000,1782403200)]
                if args.halves_only:
                    baseline = json.loads((baseline_dir/'round_1/result.json').read_text(encoding='utf-8'))
                    sets['day'] = {str(r['post_id']) for r in baseline['posts']}
                    state['baseline']={'path':str(baseline_dir/'round_1/result.json'),'count':len(sets['day']),'reference_collected_at':json.loads((baseline_dir/'status.json').read_text(encoding='utf-8')).get('rounds',[{}])[0].get('finished_at'),'noncontemporaneous_comparison':True}
                    segment_plan = segment_plan[1:]
                for label, lower, upper in segment_plan:
                    directory = out / label; directory.mkdir(exist_ok=True)
                    query = f'gpt since_time:{lower} until_time:{upper}'
                    c.build_search_query = lambda *args, **kwargs: query
                    def inside(stamp, *args, **kwargs):
                        try:
                            return lower <= datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp() < upper
                        except (ValueError, TypeError):
                            return False
                    c.post_in_date_range = inside
                    events, responses = [], []
                    def record(request, event, status=None):
                        if 'SearchTimeline' not in request.url:
                            return
                        events.append({'at':c.now_iso(),'event':event,'status':status,'query':c._search_raw_query(request.url,request.post_data),'cursor':c._search_request_cursor(request.url,request.post_data),'failure':request.failure if event=='failed' else None})
                        c._write_json_atomic(directory/'network_events.json',events)
                    def request_handler(request): record(request,'request')
                    def response_handler(response): record(response.request,'response',response.status)
                    def failed_handler(request): record(request,'failed')
                    page.on('request',request_handler); page.on('response',response_handler); page.on('requestfailed',failed_handler)
                    def checked(response):
                        payload = original_check(response)
                        rows, cursor = c.parse_search_timeline(payload,'gpt',1)
                        filename = f'response_{len(responses)+1:04d}.json'
                        c._write_json_atomic(directory/filename,payload)
                        responses.append({'at':c.now_iso(),'file':filename,'parsed_count':len(rows),'request_cursor':c._search_request_cursor(response.request.url,response.request.post_data),'next_cursor':cursor,'rate_limit':c._rate_limit_state(c._response_headers(response))})
                        c._write_json_atomic(directory/'responses_manifest.json',responses)
                        return payload
                    c.checked_search_response = checked
                    def next_with_bounded_browser_fallback(target_page, expected_query, attempts=3, timeout_ms=15000, expected_cursor=None, buffered_responses=None):
                        response, used = original_next(target_page,expected_query,attempts,timeout_ms,expected_cursor,buffered_responses)
                        if response is not None or not args.halves_only:
                            return response,used
                        # The UI may stop issuing requests at an empty tail. A
                        # single fresh browser-created cursor request supplies
                        # actual evidence; errors still reach normal protection.
                        routed=[]
                        def redirect(route):
                            request=route.request
                            if not routed and c._search_raw_query(request.url,request.post_data)==expected_query and c._search_request_cursor(request.url,request.post_data)=='':
                                routed.append(True)
                                route.continue_(url=c._search_url_with_cursor(request.url,expected_cursor))
                            else:
                                route.continue_()
                        time.sleep(cfg['graphql_delay_max'])
                        target_page.route('**/*SearchTimeline*',redirect)
                        try:
                            with target_page.expect_response(lambda r:c._search_response_matches(r,expected_query,expected_cursor),timeout=60000) as pending:
                                target_page.reload(wait_until='domcontentloaded',timeout=60000)
                            fresh=c._take_buffered_response(pending.value,buffered_responses)
                            print('[分页核验] UI触发未取得响应，已取得同查询/游标的新浏览器请求响应；不预判空页。',flush=True)
                            return fresh,used
                        finally:
                            target_page.unroute('**/*SearchTimeline*',redirect)
                    c._next_browser_signed_search_response=next_with_bounded_browser_fallback
                    def checkpoint(rows, progress):
                        c._write_json_atomic(directory/'checkpoint.json',{'posts':rows,'progress':progress})
                        state.update(state='collecting',segment=label,progress={k:v for k,v in progress.items() if k!='last_cursor'}); save()
                    rows, coverage = c.collect_search_graphql(page,'gpt',1,0,cfg['max_scrolls'],directory,date(2026,6,25),date(2026,6,25),search_type=cfg['search_type'],no_growth_stop=cfg['no_growth_stop'],request_delay_min=cfg['graphql_delay_min'],request_delay_max=cfg['graphql_delay_max'],timezone_offset=8,pagination_trigger_attempts=3,pagination_trigger_timeout_ms=15000,checkpoint=checkpoint)
                    c._write_json_atomic(directory/'result.json',{'posts':rows,'coverage':coverage})
                    page.screenshot(path=str(directory/'end_page.png'),full_page=False)
                    c._write_json_atomic(directory/'end_page_text.json',{'text':page.locator('body').inner_text(timeout=5000)})
                    page.remove_listener('request',request_handler); page.remove_listener('response',response_handler); page.remove_listener('requestfailed',failed_handler)
                    sets[label] = {str(r['post_id']) for r in rows}
                    last = responses[-1] if responses else {}
                    subsequent = [e for e in events if e['event']=='request' and e['cursor']==last.get('next_cursor') and e['at']>=last.get('at','')]
                    empty_tail=0
                    for response_info in reversed(responses):
                        if response_info['parsed_count'] != 0: break
                        empty_tail+=1
                    terminal_verified = coverage['date_rejected_count']==0 and (not last.get('next_cursor') or empty_tail>=3)
                    state['segments'].append({'segment':label,'count':len(sets[label]),'stop':coverage['stop_reason'],'last_page_parsed':last.get('parsed_count'),'last_has_cursor':bool(last.get('next_cursor')),'next_cursor_request_count':len(subsequent),'consecutive_successful_empty_responses':empty_tail,'empirical_tail_verified':terminal_verified})
                    save()
                    deadline = float(last.get('rate_limit',{}).get('reset') or 0)
                    time.sleep(cfg['delay_max'])
                    while time.time()<deadline: time.sleep(min(15,deadline-time.time()))
                halves = sets['first_half'] | sets['second_half']
                comparison = {'day_count':len(sets['day']),'half_union_count':len(halves),'only_halves':sorted(halves-sets['day']),'only_day':sorted(sets['day']-halves),'equal_sets':sets['day']==halves,'all_half_tails_verified':all(s.get('empirical_tail_verified') for s in state['segments'] if s['segment']!='day'),'noncontemporaneous_comparison':args.halves_only,'platform_all_posts_proven':False}
                c._write_json_atomic(out/'comparison.json',comparison)
                state.update(state='experiment_finished_not_platform_all_posts_proof',comparison=comparison)
            finally:
                context.close()
    except Exception as exc:
        state.update(state='blocked_no_automatic_restart',error=f'{type(exc).__name__}: {exc}',guard_hold={'at':c.now_iso(),'error':str(exc)})
    finally:
        c.build_search_query,c.checked_search_response,c.post_in_date_range=original_builder,original_check,original_filter
        c._next_browser_signed_search_response=original_next
        state['finished_at']=c.now_iso(); save()


if __name__ == '__main__':
    main()
