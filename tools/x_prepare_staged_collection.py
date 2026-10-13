"""Prepare new user-authorized lowercase collection scopes without editing old runs."""
import csv
import json
from datetime import date,timedelta
from pathlib import Path
from x_export_column_txt import ROOT,RUNS

def days(start,end):
    while start<=end:
        yield start.isoformat(); start+=timedelta(days=1)

def main():
    out=ROOT/'outputs/staged_collection_20261008'; out.mkdir(parents=True,exist_ok=True)
    overview=[]
    for batch,run_id in RUNS.items():
        folder=ROOT/'outputs/x_keyword_runs'/run_id
        raw=json.loads((folder/'raw.json').read_text(encoding='utf-8-sig'))
        keyword=raw['requested_keywords'][0].lower(); latest={}
        for r in raw['search_runs']: latest[r.get('slice_start')]=r
        import collections
        from datetime import datetime,timezone
        counts=collections.Counter(datetime.fromisoformat(p['posted_at'].replace('Z','+00:00')).astimezone(timezone(timedelta(hours=8))).date().isoformat() for p in raw['posts'])
        queue=[]; retained=[]
        for day in days(date(2026,1,1),date(2026,9,30)):
            last=latest.get(day,{}); reason=last.get('stop_reason','')
            if reason=='cursor_exhausted' and counts[day]!=40:
                retained.append(day); continue
            queue.append({'keyword':keyword,'slice_start':day,'slice_end':day,'stage':'2026','priority':0 if not counts[day] else 1 if counts[day]==40 else 2,'prior_saved_posts':counts[day],'prior_stop_reason':reason})
        queue.sort(key=lambda r:(r['priority'],r['slice_start']))
        queue.extend({'keyword':keyword,'slice_start':day,'slice_end':day,'stage':'2025','priority':3} for day in days(date(2025,6,1),date(2025,12,31)))
        plan={'original_run_id':run_id,'account_node':batch.lower(),'keyword':keyword,'requested_2026_start':'2026-01-01','requested_2026_end':'2026-09-30','invalid_requested_date_resolution':'September 31 interpreted as September 30; explicitly disclosed to user','requested_2025_start':'2025-06-01','requested_2025_end':'2025-12-31','retained_cursor_dates_2026':retained,'original_files_unchanged':True,'repair_policy':'coverage_first','stage_gate':'2025 only after all 2026 queued slices have cursor_exhausted; heuristic/technical stops remain review-required, not full completion','queue':queue}
        if batch=='D':
            plan['supplement_baseline']='outputs/quality_iteration_20261008/d_supplement/supplement_raw.json'
        dest=out/batch.lower(); dest.mkdir(exist_ok=True)
        (dest/'collection_plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        overview.append({'batch':batch,'keyword':keyword,'2026_recheck_dates':sum(r['stage']=='2026' for r in queue),'2026_retained_cursor_dates':len(retained),'2025_dates':214,'launch_state':'waiting_existing_D_supplement' if batch=='D' else 'ready'})
    (out/'plan_summary.json').write_text(json.dumps(overview,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(overview,ensure_ascii=False))

if __name__=='__main__': main()
