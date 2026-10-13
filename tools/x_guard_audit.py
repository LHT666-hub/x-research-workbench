"""Read-only collector audit; writes only its own reports, never collector state."""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import time
from x_keyword_collector import validated_empty_tail, classify_search_run, search_end_verified

RUNS = dict(a='20260928_122030', b='20260928_233433_b', c='20260929_001443_c',
            d='20260929_002436_d', e='20260929_105257_e')


def coverage(config, keywords, runs):
    grouped = collections.defaultdict(list)
    for run in runs:
        grouped[(run.get('keyword'), run.get('slice_start'), run.get('slice_end'))].append(run)
    rows = []
    for keyword in keywords:
        day = dt.date.fromisoformat(config['start_date'])
        end = dt.date.fromisoformat(config['end_date'])
        while day <= end:
            last = min(end, day + dt.timedelta(days=max(1, config.get('slice_days', 1)) - 1))
            attempts = grouped.get((keyword, day.isoformat(), last.isoformat()), [])
            reliable = any(search_end_verified(r) for r in attempts)
            count = max((int(r.get('count', 0) or 0) for r in attempts), default=0)
            state = ('reliable_nonzero' if count else 'normal_empty') if reliable else (
                'partial_sample' if count else 'failed_zero' if attempts else 'unvisited')
            if attempts and attempts[-1].get('stop_reason') == 'in_progress':
                state = 'in_progress'
            if attempts and not count and not reliable and attempts[-1].get('stop_reason')=='cursor_exhausted':
                state = 'recorded_empty_unverified'
            rows.append(dict(keyword=keyword, start=day.isoformat(), end=last.isoformat(),
                             state=state, max_attempt_posts=count, attempts=len(attempts),
                             validated_empty_tail=any(r.get('stop_reason')=='validated_empty_tail' and validated_empty_tail(r) for r in attempts),
                             latest_classification=classify_search_run(attempts[-1]) if attempts else {'state_code':'unvisited','search_end_verified':False},
                             reasons=list(dict.fromkeys(r.get('stop_reason', 'unknown') for r in attempts))))
            day = last + dt.timedelta(days=1)
    return rows


def read_json(path):
    # Atomic collector writes allow a consistent individual file; report disagreement, never overwrite.
    return json.loads(path.read_text(encoding='utf-8-sig'))


def audit(directory, processes, previous, now):
    config = read_json(directory / 'run_config.json')
    raw = read_json(directory / 'raw.json')
    status = read_json(directory / 'status.json')
    queue = read_json(directory / 'repair_queue.json')
    keywords = [v.strip() for v in (directory / 'input_keywords.txt').read_text(encoding='utf-8-sig').splitlines()
                if v.strip() and not v.strip().startswith('#')]
    rows = coverage(config, keywords, raw.get('search_runs', []))
    logpath = directory / 'collector.log'
    with logpath.open('rb') as stream:
        stream.seek(max(0, logpath.stat().st_size - 65536))
        log = stream.read().decode('utf-8', errors='replace')
    lines = log.splitlines()
    current = next((line.strip() for line in reversed(lines) if '[搜索片 ' in line), '')
    slice_index = max((i for i, line in enumerate(lines) if '[搜索片 ' in line), default=-1)
    pages = [line.strip() for line in lines[slice_index + 1:] if 'JSON页 ' in line]
    last_page = pages[-1] if pages else ''
    matching = [p for p in processes if directory.name in (p.get('CommandLine') or '')]
    fingerprint = [logpath.stat().st_size, current, last_page, len(raw.get('search_runs', [])), len(raw.get('posts', []))]
    unchanged = previous.get('fingerprint') == fingerprint
    stagnant_since = previous.get('stagnant_since', now) if unchanged else now
    pause_line = next((line for line in reversed(lines) if '暂停' in line), '')
    pause_index = max((i for i, line in enumerate(lines) if '暂停' in line), default=-1)
    page_index = max((i for i, line in enumerate(lines) if 'JSON页 ' in line), default=-1)
    minutes = re.search(r'暂停\s*([0-9.]+)\s*分钟', pause_line)
    # Conservative inferred deadline only; not an authoritative reset timestamp.
    inferred_until = logpath.stat().st_mtime + float(minutes[1]) * 60 if minutes and pause_index > page_index else 0
    issues = []
    page_number = re.search(r'JSON页\s*(\d+)/', last_page)
    previous_page = re.search(r'JSON页\s*(\d+)/', previous.get('last_page', ''))
    if current and current == previous.get('current_slice') and page_number and previous_page and int(page_number[1]) < int(previous_page[1]):
        issues.append('same_slice_page_counter_reset_possible_inflight_loss')
    counts = dict(collections.Counter(row['state'] for row in rows))
    if not matching and not raw.get('finalized'):
        issues.append('collector_stopped_unfinished')
    if matching and now > inferred_until + 120 and now - stagnant_since > 1200:
        issues.append('no_observed_progress_20m_verify_browser_and_cooldown')
    saved = len(raw.get('posts', []))
    same_processes = set(previous.get('process_ids', [])) == {p['ProcessId'] for p in matching}
    saved_since = previous.get('saved_since', now) if same_processes and previous.get('saved_posts') == saved else now
    if matching and now - saved_since > 7200 and saved > 0:
        issues.append('no_new_posts_persisted_2h_check_inflight_retries')
    zero_growth_streak = 0
    validation_time = raw.get('quality_validation', {}).get('checked_at', '')
    for run in reversed(raw.get('search_runs', [])):
        if validation_time and str(run.get('completed_at', '')) < validation_time:
            break
        if int(run.get('count', 0) or 0) == 0 and run.get('stop_reason') in ('no_new_results', 'date_filter_mismatch'):
            zero_growth_streak += 1
        else:
            break
    if zero_growth_streak >= 3:
        issues.append('consecutive_zero_growth_quality_review')
    if raw.get('guard_hold'):
        issues.append('quality_hold_do_not_auto_resume')
    tail = '\n'.join(lines[-20:])
    for label, pattern in [('searchtimeline_404', r'SearchTimeline[^\n]*404'),
                           ('browser_disconnected', r'TargetClosedError|Browser.*closed|浏览器.*断'),
                           ('api_error', r'SearchTimeline 返回接口错误'),
                           ('empty_response_with_cursor', r'SearchTimeline 连续空响应且含游标'),
                           ('login_or_challenge_check', r'登录失效|需要.*登录|验证码|login required')]:
        if re.search(pattern, tail, re.I):
            issues.append(label)
    mandatory = [row for row in rows if row['state'] in ('failed_zero', 'unvisited','recorded_empty_unverified')]
    queue_keys = {(r.get('keyword'), r.get('slice_start'), r.get('slice_end')) for r in queue}
    omitted_failures = [row for row in rows if row['state'] == 'failed_zero' and
                        (row['keyword'], row['start'], row['end']) not in queue_keys]
    if omitted_failures:
        issues.append('failed_zero_missing_from_repair_queue')
    unsettled = [row for row in rows if row['state'] == 'in_progress']
    if raw.get('finalized') and (mandatory or unsettled or queue or matching):
        issues.append('false_completion')
    if config.get('search_engine') != 'graphql' or config.get('author_strategy') != 'deferred' or config.get('repair_policy') != 'coverage_first':
        issues.append('config_drift')
    if any('--pagination-trigger-attempts 3' not in (p.get('CommandLine') or '') or '--pagination-trigger-timeout-ms 15000' not in (p.get('CommandLine') or '') for p in matching):
        issues.append('command_drift')
    if status.get('post_count') != saved:
        issues.append('snapshot_files_disagree_recheck_before_action')
    streaks = {issue: previous.get('issue_streaks', {}).get(issue, 0) + 1 for issue in issues}
    immediate = {'false_completion', 'failed_zero_missing_from_repair_queue', 'quality_hold_do_not_auto_resume', 'searchtimeline_404', 'login_or_challenge_check'}
    notifications = [issue for issue in issues if streaks[issue] == (1 if issue in immediate else 3)]
    return dict(run_id=directory.name, checked_at=now, process_ids=[p['ProcessId'] for p in matching],
                finalized=bool(raw.get('finalized')), saved_posts=saved, saved_since=saved_since,
                fingerprint=fingerprint, stagnant_since=stagnant_since, current_slice=current,
                last_page=last_page, inferred_cooldown_until=inferred_until,
                coverage_counts=counts, dates=rows, mandatory_dates=mandatory,
                omitted_failed_dates=omitted_failures,
                repair_queue_count=len(queue), issues=issues, issue_streaks=streaks,
                zero_growth_streak=zero_growth_streak, notification_events=notifications,
                verified_end_date_count=sum(row['state'] in ('reliable_nonzero','normal_empty') for row in rows),
                all_search_ends_verified=bool(rows) and all(row['state'] in ('reliable_nonzero','normal_empty') for row in rows),
                completion_basis='coverage_first_sampling_not_platform_all_posts',
                complete=bool(raw.get('finalized')) and not mandatory and not unsettled and not queue and not matching and not raw.get('guard_hold'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = subprocess.run(['powershell', '-NoProfile', '-Command',
        "@(Get-CimInstance Win32_Process | Where-Object {$_.Name -match 'python' -and $_.CommandLine -match 'x_keyword_collector.py'} | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
    processes = json.loads(result.stdout or '[]')
    if isinstance(processes, dict):
        processes = [processes]
    report_dir = args.root / 'outputs' / 'guard_audit'
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / 'latest.json'
    previous = read_json(report_path) if report_path.exists() else {}
    reports = {}
    for node, run in RUNS.items():
        try:
            reports[node] = audit(args.root / 'outputs' / 'x_keyword_runs' / run, processes, previous.get(node, {}), time.time())
        except Exception as exc:
            reports[node] = dict(run_id=run, issues=['audit_read_failed'], error=type(exc).__name__, complete=False)
        print(json.dumps({k: v for k, v in reports[node].items() if k not in ('dates', 'mandatory_dates', 'fingerprint')}, ensure_ascii=False))
    tmp = report_path.with_suffix('.tmp')
    tmp.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(report_path)
    with (report_dir / 'history.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({node: {k: v for k, v in report.items() if k not in ('dates', 'mandatory_dates')} for node, report in reports.items()}, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
