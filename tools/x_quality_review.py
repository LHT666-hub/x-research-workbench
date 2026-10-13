"""Reclassify ambiguous zero-growth records without editing search history or posts.

Run without --apply to preview. Applying requires all five collectors to be stopped.
Original snapshots are copied to a timestamped backup before regenerating outputs.
"""
import argparse
import json
import shutil
import subprocess
import time
from datetime import date
from pathlib import Path

import x_keyword_collector as collector
from x_guard_audit import RUNS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--release-after-validation', type=date.fromisoformat)
    args = parser.parse_args()
    result = subprocess.run(['powershell', '-NoProfile', '-Command',
        "@(Get-CimInstance Win32_Process | Where-Object {$_.Name -match 'python' -and $_.CommandLine -match 'x_keyword_collector.py'} | Select-Object CommandLine) | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding='utf-8', errors='replace', check=True)
    processes = json.loads(result.stdout or '[]')
    if isinstance(processes, dict):
        processes = [processes]
    if any(run in (p.get('CommandLine') or '') for run in RUNS.values() for p in processes):
        raise RuntimeError('先安全暂停全部五批，禁止修改运行中快照。')
    stamp = collector.now_iso().replace(':', '').replace('+', '_')
    validations = {}
    if args.release_after_validation:
        if args.apply:
            raise RuntimeError('审核暂停与解除保护不能同时执行。')
        for node in RUNS:
            evidence = collector.ROOT / 'tmp' / ('diagnostic-search-' + node + '.json')
            if time.time() - evidence.stat().st_mtime > 1800:
                raise RuntimeError('诊断证据已过期，必须重新核验。')
            data = json.loads(evidence.read_text(encoding='utf-8'))
            collector.validate_search_payload(data)
            rows, _ = collector.parse_search_timeline(data, node, 1)
            hits = sum(collector.post_in_date_range(str(r.get('posted_at','')), args.release_after_validation, args.release_after_validation, 8) for r in rows)
            if not rows or hits != len(rows):
                raise RuntimeError('诊断响应为空或存在日期错位，禁止解除保护。')
            validations[node] = dict(parsed=len(rows), in_range=hits, evidence=str(evidence),
                                     query_encoding='unix_time_half_open_local_calendar', day=args.release_after_validation.isoformat(), checked_at=collector.now_iso())
    for node, run in RUNS.items():
        directory = collector.OUTPUT_ROOT / run
        raw = json.loads((directory / 'raw.json').read_text(encoding='utf-8'))
        config = json.loads((directory / 'run_config.json').read_text(encoding='utf-8'))
        if config.get('repair_policy') != 'coverage_first':
            raise RuntimeError('禁止改变覆盖优先采样策略。')
        queue = collector.search_repair_queue(raw.get('search_runs', []), 'coverage_first')
        ambiguous = [r for r in raw.get('search_runs', []) if r.get('stop_reason') == 'no_new_results' and not int(r.get('count', 0) or 0)]
        backup = directory / 'quality_review_backups' / stamp
        if args.release_after_validation:
            hold = raw.get('guard_hold')
            if not isinstance(hold, dict) or hold.get('reason') != 'legacy_zero_growth_requires_review':
                raise RuntimeError('未知或新的保护原因，禁止批量解除。')
            backup = directory / 'quality_release_backups' / stamp
            backup.mkdir(parents=True, exist_ok=False)
            for name in ('raw.json', 'status.json', 'repair_queue.json', 'run_config.json'):
                shutil.copy2(directory / name, backup / name)
            raw.setdefault('guard_hold_history', []).append(raw.pop('guard_hold'))
            raw['quality_validation'] = validations[node]
            raw['active_phase'] = 'resume_pending'
            raw['finalized'] = False
            collector.save_checkpoint(directory, raw)
        if args.apply and ambiguous:
            backup.mkdir(parents=True, exist_ok=False)
            for name in ('raw.json', 'status.json', 'repair_queue.json', 'run_config.json', 'index.html'):
                if (directory / name).exists():
                    shutil.copy2(directory / name, backup / name)
            # Preserve original attempts and all saved posts; only completion/hold metadata changes.
            raw['guard_hold'] = dict(reason='legacy_zero_growth_requires_review', detected_at=collector.now_iso(),
                                     message='旧零条无增长记录不能证明正常空结果；先核验日期查询与响应，禁止自动恢复。')
            raw['finalized'] = False
            raw['active_phase'] = 'paused_quality_review'
            raw['repair_policy'] = 'coverage_first'
            raw['repair_queue_count'] = len(queue)
            raw['data_quality_warning'] = '旧零条 no_new_results 已列为待核验；有样本日期不自动重跑，真正空响应 cursor_exhausted 不重跑。'
            collector.save_outputs(directory, raw)
        print(json.dumps(dict(node=node, saved_posts=len(raw.get('posts', [])), ambiguous_attempts=len(ambiguous),
                              repair_dates=len(queue), applied=args.apply and bool(ambiguous), released=bool(args.release_after_validation), backup=str(backup) if args.release_after_validation or args.apply and ambiguous else ''), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
