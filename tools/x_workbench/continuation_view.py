"""Read-only view of one exact-cursor plan; no account or task mutations."""
import json
import re
import subprocess
from pathlib import Path

from .evidence import EvidenceCache, evidence_cache_scope
from .policy import classify_search_run
from .state import read_state


def worker_observations(root):
    command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); @(Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^python' -and $_.CommandLine -match 'x_continue_saved_cursors.py'} | Select-Object ProcessId,ParentProcessId,CommandLine) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell','-NoProfile','-Command',command],capture_output=True,text=True,encoding='utf-8',check=True,timeout=8)
    processes = json.loads(result.stdout.strip() or '[]')
    if isinstance(processes,dict): processes=[processes]
    output={}
    expected=str(Path(root).resolve()).replace('\\','/').casefold()
    for node in 'abcde':
        matches=[p for p in processes if expected in (p.get('CommandLine') or '').replace('\\','/').casefold()
                 and re.search(r'--account-node\s+'+node+r'(?:\s|$)',p.get('CommandLine') or '')]
        parents={p['ParentProcessId'] for p in matches}
        leaves=[p['ProcessId'] for p in matches if p['ProcessId'] not in parents]
        output[node]={'worker_alive':bool(leaves),'worker_pids':leaves,'duplicate_workers':len(leaves)>1}
    return output


def plan_paths(root, node):
    if node not in "abcde" or len(node) != 1:
        raise ValueError("Unknown continuation account")
    folder = Path(root) / node
    return folder / "continuation_plan.json", folder / "collection/continuation_raw.json"


def continuation_view(root, node, *, preview_limit=0, live=False):
    plan_path, path = plan_paths(root, node)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    snapshot = read_state(path, preview_limit=preview_limit, live=live)
    raw = snapshot.state
    if plan.get("account_node") != node or raw.get("account_node", node) != node:
        raise ValueError("Continuation account mismatch")
    import hashlib
    if raw.get("plan_sha256") != hashlib.sha256(plan_path.read_bytes()).hexdigest():
        raise ValueError("Continuation plan drift")
    runs = raw.get("search_runs", [])
    if live:
        with evidence_cache_scope(EvidenceCache()):
            classified = [{**r, **classify_search_run(r)} for r in runs]
    else:
        classified = [{**r, **classify_search_run(r)} for r in runs]
    ended = {(r.get("source_path"), r.get("source_record_index")) for r in classified if r["search_end_verified"]}
    queue = plan["queue"]
    planned_keys = {(item.get("source_path"), item.get("source_record_index")) for item in queue}
    verified = len(ended & planned_keys)
    unsettled = sum(not r.get("completed_at") for r in runs)
    complete = bool(raw.get("finalized")) and verified == len(queue) and not raw.get("guard_hold") and not unsettled
    return {
        "account_node": node, "run_id": plan["run_id"], "keywords": list(dict.fromkeys(item["keyword"] for item in queue)),
        "scope": "continuation_new_posts_only_not_historical_total", "actual_new_posts": snapshot.post_count,
        "planned_days": len(queue), "attempted_days": len({r.get("slice_start") for r in runs}),
        "verified_continuation_ends": verified, "pending_days": len(planned_keys - ended),
        "unsettled_records": unsettled, "current_date": raw.get("current_date"),
        "current_progress": raw.get("current_progress"), "active_phase": raw.get("active_phase"),
        "guard_hold": raw.get("guard_hold"), "cooldown_until_epoch": raw.get("cooldown_until_epoch"),
        "finalized": bool(raw.get("finalized")), "complete_without_process_check": complete,
        "snapshot": snapshot.receipt(), "posts_preview": raw["posts"], "coverage": classified if preview_limit else [],
        "natural_day_complete_proven": False, "platform_all_posts_proven": False,
    }


def audit_continuations(root, *, live=False):
    processes=worker_observations(root)
    rows=[]
    for node in 'abcde':
        plan,path=plan_paths(root,node)
        if not plan.exists():continue
        view=continuation_view(root,node,live=live)
        view.update(processes[node])
        view['complete']=view['complete_without_process_check'] and not view['worker_alive']
        view['issues']=(['guard_hold'] if view['guard_hold'] else [])+(['duplicate_workers'] if view['duplicate_workers'] else [])
        if view['finalized'] and not view['complete_without_process_check']:
            view['issues'].append('false_completion')
        rows.append(view)
    return rows
