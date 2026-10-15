"""policy components; no task starts on import."""
from __future__ import annotations
import re
from .clock import now_iso
from .parsing import _bottom_cursor, validate_search_payload
from .evidence import evidence_payload

RETRYABLE_SEARCH_STOP_REASONS = {"no_network_response", "pagination_not_triggered", "date_filter_mismatch"}
EMPTY_TAIL_REQUIRED_RESPONSES = 3
EMPTY_TAIL_POLICY = "empty_tail_v1"


def _cursor_only_empty_payload(payload: object) -> bool:
    """Recognise the observed cursor-only tail, not filtered/tombstone tweets."""
    try:
        instructions = payload['data']['search_by_raw_query']['search_timeline']['timeline']['instructions']
    except (KeyError, TypeError):
        return False
    if not isinstance(instructions, list) or not instructions or not _bottom_cursor(payload):
        return False
    for instruction in instructions:
        if not isinstance(instruction, dict) or instruction.get('type') != 'TimelineReplaceEntry':
            return False
        entry = instruction.get('entry')
        if not isinstance(entry, dict) or not isinstance(entry.get('content'), dict):
            return False
        content = entry['content']
        if content.get('cursorType') not in {'Top', 'Bottom'} or not content.get('value'):
            return False
    return True


def validated_empty_tail(run: dict[str, object]) -> bool:
    """A label alone never clears retry: require intact raw evidence and chain."""
    proof = run.get('search_end_evidence')
    if not isinstance(proof, dict) or proof.get('policy') != EMPTY_TAIL_POLICY:
        return False
    try:
        valid_counts=int(run.get('count',0) or 0)>0 and int(run.get('date_rejected_count',0) or 0)==0
    except (ValueError,TypeError):
        return False
    if not valid_counts:
        return False
    pages = proof.get('responses', [])
    if not isinstance(pages, list) or len(pages) < EMPTY_TAIL_REQUIRED_RESPONSES:
        return False
    if len({p.get('response_file') for p in pages if isinstance(p, dict)}) != len(pages):
        return False
    previous = None
    try:
        for item in pages:
            if item['http_status'] != 200 or item['query'] != run.get('query') or not item['request_cursor_matches'] or item['parsed_count'] != 0:
                return False
            if previous is not None and item['request_cursor'] != previous:
                return False
            payload = evidence_payload(item)
            validate_search_payload(payload)
            if not _cursor_only_empty_payload(payload) or _bottom_cursor(payload) != item['next_cursor']:
                return False
            previous = item['next_cursor']
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False
    return True


def search_end_verified(run: dict[str, object]) -> bool:
    return classify_search_run(run)['search_end_verified']


def response_end_evidence_valid(run: dict[str, object]) -> bool:
    proof=run.get('search_end_evidence', {})
    try:
        rejected=int(run.get('date_rejected_count',0) or 0)
    except (ValueError,TypeError):
        return False
    if not isinstance(proof,dict) or proof.get('policy')!='response_end_v1' or rejected:
        return False
    pages=proof.get('responses',[])
    if not isinstance(pages,list) or len(pages)!=1:
        return False
    try:
        item=pages[0]
        if item['http_status']!=200 or item['query']!=run.get('query') or not item['request_cursor_matches'] or item['next_cursor']:
            return False
        payload=evidence_payload(item); validate_search_payload(payload)
        instructions=payload['data']['search_by_raw_query']['search_timeline']['timeline']['instructions']
        known={'TimelineAddEntries','TimelineReplaceEntry','TimelineTerminateTimeline','TimelineClearCache','TimelinePinEntry'}
        return isinstance(instructions,list) and bool(instructions) and all(isinstance(i,dict) and i.get('type') in known for i in instructions) and not _bottom_cursor(payload)
    except (OSError,KeyError,ValueError,TypeError,RuntimeError):
        return False


def classify_search_run(run: dict[str, object]) -> dict[str, object]:
    """Single read-only taxonomy; never infer exhaustion from sample acceptance."""
    reason=str(run.get('stop_reason') or 'unknown')
    error=' '.join(str(run.get(k,'') or '') for k in ('error','interruption_error'))
    guard=run.get('guard_hold')
    if guard:
        error+=' '+str(guard)
    query=str(run.get('query','') or '')
    try:
        rejected=int(run.get('date_rejected_count',0) or 0)
        count=int(run.get('count',0) or 0)
        invalid_counts=rejected<0 or count<0
    except (ValueError,TypeError):
        rejected=0; invalid_counts=True
    bounds=re.search(r'since:(\d{4}-\d{2}-\d{2})\s+until:(\d{4}-\d{2}-\d{2})',query)
    verified=False
    if invalid_counts:
        code,label,category='invalid_status_record','计数/状态记录损坏，禁止判断到底','quality_hold'
    elif (bounds and bounds[1]>=bounds[2]) or reason=='date_filter_mismatch' or 'date_filter_mismatch' in error or rejected>0:
        code,label,category='date_query_invalid','日期查询/响应不一致，待核验','quality_hold'
    elif re.search(r'HTTP\s*(401|403)|requires login|登录失效|验证码',error,re.I):
        code,label,category='authentication_hold','登录/权限/验证码保护','protection_hold'
    elif re.search(r'HTTP\s*404',error,re.I):
        code,label,category='endpoint_hold','搜索接口404，待核验','protection_hold'
    elif re.search(r'PermissionError|No space left|磁盘|写入失败',error,re.I):
        code,label,category='storage_failure','磁盘写入失败，不是网络结束','protection_hold'
    elif reason in {'user_paused','manual_pause'} or run.get('pause_kind')=='user':
        code,label,category='user_paused','人工暂停，未声明采完','manual_pause'
    elif guard:
        code,label,category='guard_hold','保护暂停，未声明采完','protection_hold'
    elif reason in {'rate_limited','cooldown','http_429'} or re.search(r'HTTP\s*429',error,re.I):
        code,label,category='rate_limited','合法限流等待，不是采完','cooldown'
    elif 'ERR_ABORTED' in error or reason=='request_aborted':
        code,label,category='request_aborted','请求被取消，取消来源须另查','technical_interruption'
    elif re.search(r'Target.*closed|BrowserContext.close|浏览器.*断',error,re.I):
        code,label,category='browser_disconnected','浏览器连接中断，未确认结束','technical_interruption'
    elif reason=='validated_empty_tail':
        verified=run.get('coverage_status')!='needs_retry' and validated_empty_tail(run)
        code,label,category=('validated_empty_tail','已核验空尾部（非平台全量）','verified_search_end') if verified else ('invalid_end_evidence','空尾部证据缺失/无效，待核验','quality_hold')
    elif reason=='cursor_exhausted':
        verified=run.get('coverage_status')!='needs_retry' and response_end_evidence_valid(run)
        code,label,category=('response_end','成功响应无后续游标，证据已核验','verified_search_end') if verified else ('recorded_cursor_end','旧记录称无游标，原始证据待核验','legacy_unverified')
        if not verified and isinstance(run.get('search_end_evidence'),dict) and run['search_end_evidence'].get('policy')=='response_end_v1':
            code,label,category='invalid_end_evidence','无游标结束证据不完整/无效，待核验','quality_hold'
    elif reason=='no_new_results':
        code,label,category='no_new_results','无新增保护停止，不证明空尾部','heuristic_stop'
    elif reason=='repeated_cursor':
        code,label,category='repeated_cursor','重复游标防循环停止，不证明到底','heuristic_stop'
    elif reason in {'target_reached','max_pages','max_scrolls'}:
        code,label,category=reason,{'target_reached':'达到条数目标','max_pages':'达到页数上限','max_scrolls':'达到滚动上限'}[reason]+'，未确认结束','budget_stop'
    elif reason in {'pagination_not_triggered','no_network_response'}:
        code,label,category=reason,{'pagination_not_triggered':'未取得匹配的下一页响应','no_network_response':'未取得搜索响应'}[reason]+'，未确认结束','technical_interruption'
    elif reason=='in_progress':
        code,label,category='in_progress','进行中/未结算检查点，不算完成','in_progress'
    elif reason=='interrupted_with_saved_sample':
        code,label,category='interrupted_with_saved_sample','中断且样本已保留，未确认结束','technical_interruption'
    else:
        code,label,category='unknown_stop','未知停止原因，不推断采完','unknown'
    proof=run.get('search_end_evidence',{})
    candidate=not verified and isinstance(proof,dict) and bool(proof.get('responses')) and proof.get('policy')==EMPTY_TAIL_POLICY
    underlying=classify_search_run({**run,'guard_hold':None,'error':error})['state_code'] if code=='guard_hold' else code
    return {'state_code':code,'search_end_status':code,'state_label':label,'state_category':category,'underlying_state_code':underlying,
        'search_end_verified':verified,'empty_tail_candidate':candidate,
        'search_end_scope':'exact_query_this_account_not_platform_all_posts',
        'platform_all_posts_proven':False}


def search_run_key(run: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(run.get("keyword", "")),
        str(run.get("slice_start", "")),
        str(run.get("slice_end", "")),
    )


def search_run_needs_retry(run: dict[str, object]) -> bool:
    if run.get('stop_reason') == 'validated_empty_tail':
        return not validated_empty_tail(run) or run.get('coverage_status') == 'needs_retry'
    classification=classify_search_run(run)
    if classification['state_category'] in {'quality_hold','protection_hold'}:
        return True
    if run.get('stop_reason')=='cursor_exhausted':
        return not classification['search_end_verified']
    return (
        str(run.get("stop_reason", "")) in RETRYABLE_SEARCH_STOP_REASONS
        or str(run.get("coverage_status", "")) == "needs_retry"
        # Heuristic stopping is not an end-of-search proof, even with samples.
        or run.get('stop_reason') in {'no_new_results','repeated_cursor'}
        or str(run.get("stop_reason", "")) not in {"cursor_exhausted", "no_new_results", "repeated_cursor", "target_reached", "max_pages"}
    )


def reliable_search_keys(search_runs: object) -> set[tuple[str, str, str]]:
    if not isinstance(search_runs, list):
        return set()
    return {
        search_run_key(run)
        for run in search_runs
        if isinstance(run, dict) and search_end_verified(run)
    }


def accepted_search_keys(search_runs: object) -> set[tuple[str, str, str]]:
    """Coverage-first sampling accepts partial days without claiming exhaustion."""
    keys = reliable_search_keys(search_runs)
    if isinstance(search_runs, list):
        keys.update(search_run_key(run) for run in search_runs
                    if isinstance(run, dict) and int(run.get("count", 0) or 0) > 0)
        # Preserve the user's no-auto-rerun rule for recorded zero cursor ends;
        # this is sampling acceptance, explicitly not verified exhaustion.
        keys.update(search_run_key(run) for run in search_runs if isinstance(run,dict)
                    and not int(run.get('count',0) or 0) and run.get('stop_reason')=='cursor_exhausted'
                    and classify_search_run(run)['state_code']=='recorded_cursor_end')
    return keys


def settle_saved_in_progress(search_runs: list[dict[str, object]]) -> int:
    """At resume only, settle abandoned samples without erasing their checkpoints."""
    latest = {search_run_key(run): run for run in search_runs}
    additions = []
    for run in latest.values():
        if run.get("stop_reason") == "in_progress" and int(run.get("count", 0) or 0) > 0:
            additions.append({**run, "stop_reason": "interrupted_with_saved_sample",
                              "coverage_status": "needs_retry",
                              "interruption_error": "Previous process ended before slice completion; cause unknown",
                              "resolution_kind": "abandoned_checkpoint_settlement_no_network",
                              "resolved_at": now_iso(),
                              "source_checkpoint_at": run.get("checkpoint_at", "")})
    search_runs.extend(additions)
    return len(additions)


def search_repair_queue(search_runs: object, policy: str = "strict") -> list[dict[str, object]]:
    """Return unresolved date slices that must be replayed before completion."""
    if not isinstance(search_runs, list):
        return []
    reliable = accepted_search_keys(search_runs) if policy == "coverage_first" else reliable_search_keys(search_runs)
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for run in search_runs:
        if not isinstance(run, dict) or not search_run_needs_retry(run):
            continue
        key = search_run_key(run)
        if key in reliable:
            continue
        grouped.setdefault(key, []).append(run)
    queue: list[dict[str, object]] = []
    for key, attempts in grouped.items():
        latest = attempts[-1]
        queue.append({
            "keyword": key[0],
            "keyword_index": int(latest.get("keyword_index", 0) or 0),
            "slice_start": key[1],
            "slice_end": key[2],
            "attempts": len(attempts),
            "last_stop_reason": str(latest.get("stop_reason", "")),
            "partial_post_count": max(int(run.get("count", 0) or 0) for run in attempts),
            "last_attempt_at": str(latest.get("completed_at", "")),
            "status": "pending",
        })
    return sorted(queue, key=lambda row: (int(row.get("keyword_index", 0)), str(row.get("slice_start", ""))))
