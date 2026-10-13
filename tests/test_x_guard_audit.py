import importlib.util
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
spec = importlib.util.spec_from_file_location('guard', Path(__file__).parents[1] / 'tools/x_guard_audit.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_all_expected_dates_and_unknown_reasons():
    cfg = dict(start_date='2026-01-01', end_date='2026-01-04', slice_days=1)
    runs = [dict(keyword='GPT', slice_start='2026-01-01', slice_end='2026-01-01', count=0, stop_reason='unknown'),
            dict(keyword='GPT', slice_start='2026-01-02', slice_end='2026-01-02', count=20, stop_reason='pagination_not_triggered'),
            dict(keyword='GPT', slice_start='2026-01-03', slice_end='2026-01-03', count=0, stop_reason='cursor_exhausted')]
    # Historical labels without response evidence are not verified empty days.
    assert [r['state'] for r in guard.coverage(cfg, ['GPT'], runs)] == ['failed_zero', 'partial_sample', 'recorded_empty_unverified', 'unvisited']


def test_preserves_partial_success_after_later_failure():
    cfg = dict(start_date='2026-01-01', end_date='2026-01-01', slice_days=1)
    runs = [dict(keyword='GPT', slice_start='2026-01-01', slice_end='2026-01-01', count=n, stop_reason='pagination_not_triggered') for n in [20, 0]]
    row = guard.coverage(cfg, ['GPT'], runs)[0]
    assert row['state'] == 'partial_sample' and row['attempts'] == 2


def test_legacy_zero_growth_not_false_completion():
    cfg = dict(start_date='2026-01-01', end_date='2026-01-02', slice_days=1)
    runs = [dict(keyword='GPT', slice_start='2026-01-01', slice_end='2026-01-01', count=0, stop_reason='no_new_results', coverage_status='apparently_exhausted'),
            dict(keyword='GPT', slice_start='2026-01-02', slice_end='2026-01-02', count=0, stop_reason='cursor_exhausted')]
    assert [r['state'] for r in guard.coverage(cfg, ['GPT'], runs)] == ['failed_zero', 'recorded_empty_unverified']


def test_verified_empty_response_is_normal_empty_but_missing_evidence_is_not(tmp_path):
    query = 'gpt since_time:1782316800 until_time:1782403200'
    path = tmp_path / 'response.json'
    payload = {'data': {'search_by_raw_query': {'search_timeline': {'timeline': {
        'instructions': [{'type': 'TimelineTerminateTimeline', 'direction': 'Bottom'}]}}}}}
    path.write_text(json.dumps(payload), encoding='utf-8')
    run = dict(keyword='gpt', slice_start='2026-06-25', slice_end='2026-06-25',
               query=query, count=0, stop_reason='cursor_exhausted',
               search_end_evidence={'policy': 'response_end_v1', 'responses': [{
                   'http_status': 200, 'query': query, 'request_cursor_matches': True,
                   'next_cursor': '', 'response_file': str(path),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]})
    cfg = dict(start_date='2026-06-25', end_date='2026-06-25', slice_days=1)
    assert guard.coverage(cfg, ['gpt'], [run])[0]['state'] == 'normal_empty'
    path.unlink()
    assert guard.coverage(cfg, ['gpt'], [run])[0]['state'] == 'recorded_empty_unverified'


def test_hold_survives_process_exit_and_cannot_be_complete(tmp_path):
    cfg = dict(start_date='2026-01-01', end_date='2026-01-01', slice_days=1, search_engine='graphql', author_strategy='deferred', repair_policy='coverage_first')
    raw = dict(finalized=True, guard_hold={'reason':'date_filter_mismatch'}, posts=[], search_runs=[dict(keyword='GPT',slice_start='2026-01-01',slice_end='2026-01-01',count=0,stop_reason='date_filter_mismatch',coverage_status='needs_retry')])
    for name, value in [('run_config.json',cfg),('raw.json',raw),('status.json',{'post_count':0}),('repair_queue.json',[])]:
        (tmp_path/name).write_text(json.dumps(value),encoding='utf-8')
    (tmp_path/'input_keywords.txt').write_text('GPT',encoding='utf-8')
    (tmp_path/'collector.log').write_text('',encoding='utf-8')
    report = guard.audit(tmp_path, [], {}, 10000)
    assert not report['complete']
    assert 'false_completion' in report['notification_events']
    assert 'quality_hold_do_not_auto_resume' in report['notification_events']
    again = guard.audit(tmp_path, [], report, 11000)
    assert not again['notification_events']


def test_finalized_saved_in_progress_is_not_complete(tmp_path):
    cfg = dict(start_date='2026-01-01', end_date='2026-01-01', slice_days=1,
               search_engine='graphql', author_strategy='deferred', repair_policy='coverage_first')
    run = dict(keyword='GPT', slice_start='2026-01-01', slice_end='2026-01-01',
               count=20, stop_reason='in_progress', coverage_status='needs_retry')
    raw = dict(finalized=True, posts=[], search_runs=[run])
    for name, value in [('run_config.json', cfg), ('raw.json', raw),
                        ('status.json', {'post_count': 0}), ('repair_queue.json', [])]:
        (tmp_path/name).write_text(json.dumps(value), encoding='utf-8')
    (tmp_path/'input_keywords.txt').write_text('GPT', encoding='utf-8')
    (tmp_path/'collector.log').write_text('', encoding='utf-8')
    report = guard.audit(tmp_path, [], {}, 10000)
    assert not report['complete'] and 'false_completion' in report['issues']
    raw['search_runs'].append({**run, 'stop_reason': 'interrupted_with_saved_sample'})
    (tmp_path/'raw.json').write_text(json.dumps(raw), encoding='utf-8')
    report = guard.audit(tmp_path, [], {}, 10000)
    assert report['complete'] and report['coverage_counts'] == {'partial_sample': 1}
