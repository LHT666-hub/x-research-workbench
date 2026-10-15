from __future__ import annotations

import argparse
import csv
import email.utils
import html
import hashlib
import json
import random
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from x_account_collector import CHROME, about_result, now_iso, wait_for_login
# Compatibility facade: existing script imports remain valid.
from x_workbench.query import (
    parse_date,
    build_search_query,
    post_in_date_range,
    make_date_slices,
)
from x_workbench.parsing import (
    SearchRateLimitError,
    _nested_dict,
    _unwrap_tweet_result,
    _tweet_results,
    _bottom_cursor,
    _twitter_time_to_iso,
    validate_search_payload,
    parse_search_timeline,
)
from x_workbench.request_identity import (
    _search_url_with_cursor,
    _search_raw_query,
    _search_request_cursor,
    _search_response_matches,
    _response_headers,
    _search_template,
    _graphql_operation,
)
from x_workbench.policy import (
    _cursor_only_empty_payload,
    validated_empty_tail,
    search_end_verified,
    response_end_evidence_valid,
    classify_search_run,
    search_run_key,
    search_run_needs_retry,
    reliable_search_keys,
    accepted_search_keys,
    settle_saved_in_progress,
    search_repair_queue,
)
from x_workbench.browser import (
    _BufferedSearchResponse,
    _take_buffered_response,
    _scroll_last_tweet_if_attached,
    _next_browser_signed_search_response,
    _fresh_browser_cursor_response,
)
from x_workbench.policy import RETRYABLE_SEARCH_STOP_REASONS, EMPTY_TAIL_REQUIRED_RESPONSES, EMPTY_TAIL_POLICY
from x_workbench.storage import write_json_atomic as _write_json_atomic
from x_workbench.progress import RowDeltaTracker
from x_workbench.evidence import EvidenceCache, evidence_cache_scope
from x_workbench.metrics import (
    StageTimings,
    current_timings,
    timing_scope,
    measure,
    measured_sleep,
)
from x_workbench.metrics import timed


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_DIR = ROOT / "tmp" / "chrome-x-keyword-profile"
OUTPUT_ROOT = ROOT / "outputs" / "x_keyword_runs"
SHARED_ABOUT_COOLDOWN = ROOT / "tmp" / "x-about-cooldown.json"
_STATUS_EVIDENCE_CACHE = EvidenceCache()


class RateLimitError(RuntimeError):
    def __init__(self, status: int = 429, retry_after: float | None = None, operation: str = "AboutAccountQuery") -> None:
        super().__init__(f"{operation} HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.operation = operation


def _retry_after_seconds(headers: dict[str, object]) -> float | None:
    value = str(headers.get("retry-after", "") or "").strip()
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    reset = str(headers.get("x-rate-limit-reset", "") or "").strip()
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return None


def _rate_limit_state(headers: dict[str, object]) -> dict[str, float]:
    state: dict[str, float] = {}
    for header, key in (
        ("x-rate-limit-limit", "limit"),
        ("x-rate-limit-remaining", "remaining"),
        ("x-rate-limit-reset", "reset"),
    ):
        value = str(headers.get(header, "") or "").strip()
        if not value:
            continue
        try:
            state[key] = float(value)
        except ValueError:
            continue
    return state


def set_shared_about_cooldown(seconds: float, reason: str) -> float:
    until = time.time() + max(1.0, seconds)
    SHARED_ABOUT_COOLDOWN.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    try:
        current = json.loads(SHARED_ABOUT_COOLDOWN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    until = max(until, float(current.get("until", 0) or 0)) if isinstance(current, dict) else until
    temporary = SHARED_ABOUT_COOLDOWN.with_suffix(".tmp")
    temporary.write_text(json.dumps({"until": until, "reason": reason, "updated_at": now_iso()}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SHARED_ABOUT_COOLDOWN)
    return until


def wait_for_shared_about_cooldown(payload: dict[str, object], output_dir: Path) -> None:
    try:
        state = json.loads(SHARED_ABOUT_COOLDOWN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    until = float(state.get("until", 0) or 0) if isinstance(state, dict) else 0
    remaining = until - time.time()
    if remaining <= 0:
        return
    payload["active_phase"] = "rate_limited"
    payload["cooldown_until"] = datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")
    save_outputs(output_dir, payload)
    print(f"[共享冷却] 所在地接口暂停 {remaining / 60:.1f} 分钟，预计 {payload['cooldown_until']} 恢复。", flush=True)
    time.sleep(remaining)
    payload.pop("cooldown_until", None)
    payload["active_phase"] = "enriching_authors"
    save_outputs(output_dir, payload)

POST_FIELDS = [
    "keyword", "keyword_index", "rank", "post_id", "post_url", "post_text",
    "posted_at", "time_label", "author_username", "author_display_name",
    "author_user_id", "profile_location", "profile_location_status", "profile_location_error", "profile_location_collected_at",
    "account_based_in", "source", "account_lookup_status",
    "collected_at",
]

DEDUP_FIELDS = [
    "post_id", "post_url", "post_text", "posted_at", "time_label",
    "author_username", "author_display_name", "author_user_id",
    "profile_location", "profile_location_status", "profile_location_error", "profile_location_collected_at", "account_based_in",
    "matched_keywords", "keyword_count", "source",
    "account_lookup_status", "collected_at",
]

CODING_FIELDS = DEDUP_FIELDS + [
    "include", "theme_code", "sentiment_code", "coder", "coding_notes",
]


def read_keywords(path: Path) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if len(value) > 200:
            raise ValueError(f"关键词过长：{value[:40]}…")
        if value.casefold() not in seen:
            seen.add(value.casefold())
            output.append(value)
    return output


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value).strip(" ._")
    return cleaned[:60] or "keyword"


def profile_location_needs_lookup(author: object) -> bool:
    if not isinstance(author, dict) or not str(author.get("username", "") or "").strip():
        return False
    if str(author.get("profile_location", "") or "").strip():
        return False
    return str(author.get("profile_location_status", "pending") or "pending") not in {"success", "empty"}


def profile_location_queue(authors: object) -> list[dict[str, object]]:
    if not isinstance(authors, dict):
        return []
    rows = []
    for author in authors.values():
        if not profile_location_needs_lookup(author):
            continue
        rows.append({
            "username": str(author.get("username", "") or ""),
            "user_id": str(author.get("user_id", "") or ""),
            "status": str(author.get("profile_location_status", "pending") or "pending"),
            "error": str(author.get("profile_location_error", "") or ""),
        })
    return sorted(rows, key=lambda item: str(item["username"]).casefold())


def text_or_empty(locator) -> str:
    try:
        return locator.inner_text(timeout=3000).strip() if locator.count() else ""
    except Exception:
        return ""


def wait_for_search_ready(page, timeout_seconds: int = 90) -> str:
    """Wait until X shows posts or an explicit empty state; a spinner is not empty."""
    empty_markers = (
        "No results for", "Try searching for something else", "没有找到结果",
        "找不到与此相关的结果", "未找到结果",
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.locator('article[data-testid="tweet"]').count():
            return "posts"
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        if any(marker in body for marker in empty_markers):
            return "empty"
        if "Log in to X" in body:
            raise RuntimeError("搜索页要求重新登录")
        if "Rate limit exceeded" in body:
            raise RuntimeError("搜索页触发限流")
        page.wait_for_timeout(1000)
    raise RuntimeError(f"搜索页持续加载超过 {timeout_seconds} 秒，未出现帖子或明确的无结果提示")


def extract_visible_posts(page, keyword: str, keyword_index: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    cards = page.locator('article[data-testid="tweet"]')
    for index in range(cards.count()):
        card = cards.nth(index)
        time_node = card.locator("time").first
        if not time_node.count():
            continue
        link = time_node.locator("xpath=..").get_attribute("href") or ""
        match = re.search(r"^/([^/]+)/status/(\d+)", link)
        if not match:
            continue
        author_username, post_id = match.group(1), match.group(2)
        text_node = card.locator('[data-testid="tweetText"]').first
        post_text = text_or_empty(text_node)
        if not post_text:
            continue
        user_block = text_or_empty(card.locator('[data-testid="User-Name"]').first)
        display_name = ""
        for line in user_block.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith("@") and candidate not in {"·", "Ad"}:
                display_name = candidate
                break
        posted_at = time_node.get_attribute("datetime") or ""
        rows.append({
            "keyword": keyword,
            "keyword_index": keyword_index,
            "rank": 0,
            "post_id": post_id,
            "post_url": f"https://x.com/{author_username}/status/{post_id}",
            "post_text": post_text,
            "posted_at": posted_at,
            "time_label": text_or_empty(time_node),
            "author_username": author_username,
            "author_display_name": display_name,
            "author_user_id": "",
            "profile_location": "",
            "profile_location_status": "pending",
            "profile_location_error": "",
            "profile_location_collected_at": "",
            "account_based_in": "",
            "source": "about_this_account",
            "account_lookup_status": "pending",
            "collected_at": now_iso(),
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or POST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def deduplicate_posts(posts: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge repeated keyword hits while preserving all matched keywords."""
    merged: dict[str, dict[str, object]] = {}
    keyword_lists: dict[str, list[str]] = {}
    for index, post in enumerate(posts):
        identity = str(post.get("post_id") or post.get("post_url") or f"row-{index}")
        if identity not in merged:
            merged[identity] = dict(post)
            keyword_lists[identity] = []
        keyword = str(post.get("keyword", "")).strip()
        if keyword and keyword.casefold() not in {item.casefold() for item in keyword_lists[identity]}:
            keyword_lists[identity].append(keyword)
        current = merged[identity]
        for field in (
            "author_user_id", "profile_location", "profile_location_status", "profile_location_error", "profile_location_collected_at",
            "account_based_in", "author_display_name",
        ):
            if not current.get(field) and post.get(field):
                current[field] = post[field]
        if current.get("account_lookup_status") in {"pending", "failed", ""} and post.get("account_lookup_status") in {"success", "unknown"}:
            current["account_lookup_status"] = post["account_lookup_status"]
    output: list[dict[str, object]] = []
    for identity, post in merged.items():
        keywords = keyword_lists[identity]
        row = {field: post.get(field, "") for field in DEDUP_FIELDS}
        row["matched_keywords"] = " | ".join(keywords)
        row["keyword_count"] = len(keywords)
        output.append(row)
    return output


def count_rows(values: list[str], label: str) -> list[dict[str, object]]:
    counter = Counter(value.strip() or "Unknown" for value in values)
    total = sum(counter.values()) or 1
    return [
        {label: name, "count": count, "percent": round(count * 100 / total, 2)}
        for name, count in counter.most_common()
    ]


def bar_chart_svg(rows: list[dict[str, object]], label: str, title: str, limit: int = 8) -> str:
    selected = rows[:limit]
    if not selected:
        return "<p class='empty'>暂无数据</p>"
    width, left, right, row_height = 680, 150, 54, 37
    height = 34 + row_height * len(selected)
    maximum = max(int(row["count"]) for row in selected) or 1
    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{html.escape(title)}'>"]
    for index, row in enumerate(selected):
        y = 20 + index * row_height
        value = int(row["count"])
        bar_width = max(3, (width - left - right) * value / maximum)
        name = html.escape(str(row[label]))
        parts.append(f"<text x='{left - 10}' y='{y + 15}' text-anchor='end'>{name[:24]}</text>")
        parts.append(f"<rect x='{left}' y='{y}' width='{bar_width:.1f}' height='20' rx='4'></rect>")
        parts.append(f"<text class='value' x='{left + bar_width + 8:.1f}' y='{y + 15}'>{value}</text>")
    parts.append("</svg>")
    return "".join(parts)


def timeline_chart_svg(rows: list[dict[str, object]]) -> str:
    selected = rows[-14:]
    if not selected:
        return "<p class='empty'>暂无带日期的帖子</p>"
    width, height, left, right, top, bottom = 900, 230, 50, 25, 25, 48
    maximum = max(int(row["count"]) for row in selected) or 1
    span = max(1, len(selected) - 1)
    points = []
    labels = []
    for index, row in enumerate(selected):
        x = left + (width - left - right) * index / span
        y = top + (height - top - bottom) * (1 - int(row["count"]) / maximum)
        points.append(f"{x:.1f},{y:.1f}")
        labels.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4'></circle><text x='{x:.1f}' y='{height - 18}' text-anchor='middle'>{html.escape(str(row['date'])[5:])}</text><text class='value' x='{x:.1f}' y='{y - 9:.1f}' text-anchor='middle'>{row['count']}</text>")
    return f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='帖子时间分布'><polyline class='timeline' points='{' '.join(points)}'></polyline>{''.join(labels)}</svg>"


def report_html(posts: list[dict[str, object]], payload: dict[str, object], run_id: str = "") -> str:
    deduped = deduplicate_posts(posts)
    countries = count_rows([str(row.get("account_based_in", "")) for row in deduped], "country")
    keywords = count_rows([str(row.get("keyword", "")) for row in posts], "keyword")
    authors = count_rows([str(row.get("author_username", "")) for row in deduped], "author")
    dates = count_rows([str(row.get("posted_at", ""))[:10] for row in deduped if row.get("posted_at")], "date")
    dates.sort(key=lambda row: str(row["date"]))
    coverage = [run for run in payload.get("search_runs", []) if isinstance(run, dict)]
    exhausted_count = sum(search_end_verified(run) for run in coverage)
    truncated_count = sum(run.get("coverage_status") in {"truncated", "needs_retry"} for run in coverage)
    coverage_rows = "".join(
        "<tr><td>{}</td><td>{} — {}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><span class='tag {}'>{}</span></td></tr>".format(
            html.escape(str(run.get("keyword", ""))), html.escape(str(run.get("slice_start", "开放")) or "开放"),
            html.escape(str(run.get("slice_end", "开放")) or "开放"), run.get("count", 0),
            f"{run.get('operation_name', 'SearchTimeline')} · JSON {run.get('json_pages_used', 0)} 页" if run.get("search_engine") == "graphql" else f"滚动 {run.get('scrolls_used', '')} 次",
            f"{run.get('elapsed_seconds', 0):.1f} 秒 · {run.get('posts_per_minute', 0):.1f} 条/分" if isinstance(run.get("elapsed_seconds"), (int, float)) else "旧版未记录",
            html.escape(str(run.get("stop_reason", "旧版记录"))), "ok" if search_end_verified(run) else "warn",
            html.escape(str(classify_search_run(run)['state_label'])),
        ) for run in coverage
    ) or "<tr><td colspan='7'>暂无覆盖记录</td></tr>"
    known = sum(bool(row.get("account_based_in")) for row in deduped)
    known_rate = round(known * 100 / len(deduped), 1) if deduped else 0
    known_countries = [row for row in countries if str(row["country"]) != "Unknown"]
    country_options = "".join(
        f"<option value='{html.escape(str(row['country']).casefold(), quote=True)}'>{html.escape(str(row['country']))} ({row['count']})</option>"
        for row in known_countries
    )
    keyword_names = sorted({str(post.get("keyword", "")) for post in posts if post.get("keyword")}, key=str.casefold)
    keyword_options = "".join(
        f"<option value='{html.escape(name.casefold(), quote=True)}'>{html.escape(name)}</option>" for name in keyword_names
    )
    resolved_authors = len([row for row in payload.get("authors", {}).values() if isinstance(row, dict) and row.get("status") != "pending"]) if isinstance(payload.get("authors"), dict) else 0
    unique_author_count = len({str(row.get("author_username", "")).casefold() for row in deduped if row.get("author_username")})
    if payload.get("active_phase") == "completed_with_author_backlog":
        location_notice = f"帖子采集已完成：已安全保存 {unique_author_count} 位作者的用户名、稳定用户 ID（如搜索响应提供）和自填位置；Account based in 尚待后台补全。"
    elif payload.get("finalized"):
        location_notice = f"作者所在地核验已完成：已查询 {resolved_authors} 位去重作者；没有国家值的账号表示 X 未公开或查询未成功。"
    else:
        location_notice = f"本批次尚未完成，当前已查询 {resolved_authors} / {unique_author_count} 位去重作者。没有国家值的帖子显示为“待查询”，不再误标为 Unknown。"
    quality_warning = str(payload.get("data_quality_warning", "")).strip()
    if quality_warning:
        location_notice = f"数据质量警告：{quality_warning}　{location_notice}"
    author_rows = "".join(
        f"<tr><td>@{html.escape(str(row['author']))}</td><td>{row['count']}</td><td>{row['percent']}%</td></tr>" for row in authors[:10]
    ) or "<tr><td colspan='3'>暂无数据</td></tr>"
    # Small reports remain self-contained. Large reports load the adjacent JSON
    # file so the HTML/DOM stays responsive instead of embedding megabytes.
    embedded = json.dumps(deduped, ensure_ascii=False).replace("</", "<\\/") if len(deduped) <= 500 else "[]"
    template = r"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>X 关键词研究工作台</title>
<style>:root{--navy:#132433;--navy2:#1c3447;--blue:#1677ff;--bg:#f3f6f9;--panel:#fff;--line:#d8e2eb;--text:#172033;--muted:#657789;--green:#16865d}*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,'Microsoft YaHei',sans-serif}.shell{display:grid;grid-template-columns:240px 1fr;height:100%}aside{position:sticky;top:0;height:100vh;background:var(--navy);color:white;padding:24px 18px}.brand{font-weight:800;font-size:19px;margin:0 8px 28px}.brand small{display:block;color:#aac0d0;font-size:11px;margin-top:5px;font-weight:500}.nav a{display:block;color:#c9d8e3;text-decoration:none;padding:11px 12px;border-radius:8px;margin:4px 0}.nav a.active{background:var(--blue);color:#fff}.side-note{position:absolute;left:18px;right:18px;bottom:20px;color:#9fb4c4;font-size:12px;line-height:1.6}.content{height:100vh;overflow:auto}header{position:sticky;top:0;z-index:4;background:rgba(255,255,255,.97);border-bottom:1px solid var(--line);padding:12px 24px;display:flex;justify-content:space-between;align-items:center;gap:16px}header h1{font-size:20px;margin:0}header span{color:var(--muted);font-size:13px}.top-actions{display:flex;align-items:center;gap:9px}.top-actions select{min-width:270px}main{padding:22px 24px 40px;max-width:1600px}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:11px}.card{padding:16px}.card span{color:var(--muted);font-size:12px}.card b{display:block;font-size:28px;margin:5px 0}.card em{font-size:12px;color:var(--green);font-style:normal}.notice{margin-top:14px;padding:13px 15px;border:1px solid #efcf75;background:#fff8df;color:#735100;border-radius:10px;line-height:1.6}.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.panel{padding:18px}.panel h2{font-size:16px;margin:0 0 14px}.panel svg{width:100%;height:auto}.panel svg text{font-size:11px;fill:var(--muted)}.panel svg rect{fill:#8fbdf5}.panel svg .value{fill:var(--text);font-weight:700}.panel svg .timeline{fill:none;stroke:#1677ff;stroke-width:3}.panel svg circle{fill:#1677ff}.exports{display:flex;gap:8px;flex-wrap:wrap}.button{display:inline-block;background:var(--navy2);color:#fff;text-decoration:none;border:0;border-radius:7px;padding:9px 12px;font-weight:700;cursor:pointer;font-size:13px}.button.secondary{background:#e8eef3;color:var(--navy)}.toolbar{position:sticky;top:62px;z-index:3;background:#fff;padding:10px 0;display:grid;grid-template-columns:minmax(230px,1fr) 170px 170px 145px 145px auto;gap:9px;margin:4px 0 10px}input,select{width:100%;padding:10px 11px;border:1px solid #adbdca;border-radius:7px;background:#fff}.table{overflow:auto;border:1px solid var(--line);border-radius:9px;max-height:620px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:9px 10px;border-bottom:1px solid #e6edf2;text-align:left;vertical-align:top}th{position:sticky;top:0;background:var(--navy2);color:white;z-index:2;white-space:nowrap}.post{min-width:340px;white-space:normal;line-height:1.5}.plain{color:#1264b3}.split{display:grid;grid-template-columns:1fr 360px;gap:14px;margin-top:14px}.empty{color:var(--muted)}.count{color:var(--muted);font-size:13px;margin-left:8px}.tag{display:inline-block;padding:4px 8px;border-radius:12px;background:#fff0dd;color:#8a4c00}.tag.ok{background:#e1f5e9;color:#116d3d}.pager{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-top:10px}.pager button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:1200px){.toolbar{grid-template-columns:1fr 1fr 1fr}}@media(max-width:1000px){.shell{grid-template-columns:1fr}aside{display:none}.kpis{grid-template-columns:repeat(2,1fr)}.charts,.split{grid-template-columns:1fr}.toolbar{position:static;grid-template-columns:1fr 1fr}.content{height:auto}header{position:relative;align-items:flex-start;flex-direction:column}.top-actions{width:100%;flex-wrap:wrap}.top-actions select{min-width:220px;flex:1}}@media(max-width:600px){.kpis,.toolbar{grid-template-columns:1fr}main{padding:14px}}</style></head>
<style>:root{--navy:#2c2825;--navy2:#39322e;--blue:#c86143;--bg:#f3efe7;--panel:#fffdf9;--line:#ddd5c9;--text:#302c28;--muted:#756e66;--green:#287a55}body{background:radial-gradient(circle at 80% 0,#eadfd2 0,transparent 29%),var(--bg)}aside{background:linear-gradient(180deg,#2c2825,#211e1c);padding:28px 18px}.brand{font-family:Georgia,'Songti SC',serif;font-size:21px}.brand small{color:#c9bbae}.nav a{color:#ded5cd;border:1px solid transparent}.nav a:hover{background:#3b3531;border-color:#4b433e}.nav a.active{background:#c86143}.side-note{color:#ae9f93}header{background:rgba(248,245,238,.94);backdrop-filter:blur(14px)}header h1{font-family:Georgia,'Songti SC',serif;font-size:22px}.card,.panel{border-color:var(--line);border-radius:18px;box-shadow:0 12px 34px rgba(77,61,47,.055)}.card{padding:18px}.card b{font-family:Georgia,serif;font-size:31px}.button{border-radius:10px;background:#38312d}.button.secondary{background:#eee8df;color:#4b433d}.notice{background:#fff6dc;border-color:#e5c77c;border-radius:14px}.filter-shell{padding:16px;border:1px solid var(--line);border-radius:15px;background:#f8f5ef;margin-bottom:13px}.filter-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px}.filter-field{display:flex;flex-direction:column;gap:6px;min-width:0;color:var(--muted);font-size:12px;font-weight:700}.filter-field.wide{grid-column:span 2}.filter-field input,.filter-field select{height:42px;min-width:0;border-color:#c9bfb2;border-radius:10px;background:#fffdf9;color:var(--text)}.filter-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px}.filter-actions .hint{color:var(--muted);font-size:12px}.filter-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:0 0 13px}.filter-stat{padding:13px 15px;border-radius:13px;background:#f3eee6;border:1px solid #e2d9cd;color:var(--muted);font-size:12px}.filter-stat b{display:block;color:var(--text);font:700 22px/1.1 Georgia,serif;margin-bottom:3px}.table{border-color:var(--line);border-radius:13px}th{background:#39322e}th,td{padding:10px 11px}.pager{justify-content:space-between}.pager-center{color:var(--muted);font-size:13px}.toolbar{position:static}.plain{color:#a84f35;font-weight:700}@media(max-width:1180px){.filter-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){.filter-grid,.filter-summary{grid-template-columns:1fr}.filter-field.wide{grid-column:span 1}.filter-actions{align-items:flex-start;flex-direction:column}}</style>
<body><div class='shell'><aside data-od-id='sidebar'><div class='brand'>X Research Lab<small>关键词帖子研究工作台</small></div><nav class='nav'><a href='/'>← 返回采集工作台</a><a class='active' href='#overview'>研究概览</a><a href='#dataset'>筛选帖子</a><a href='#coverage'>覆盖诊断</a><a href='#distribution'>分组统计</a><a href='posts.csv'>原始命中</a><a href='research_coding_template.csv'>编码模板</a><a href='all_exports.zip'>完整导出</a></nav><div class='side-note'>Profile Location：用户自填<br>Account based in：X 聚合 IP 推断<br><br>页面生成时间：__UPDATED__</div></aside>
<div class='content'><header data-od-id='topbar'><div><h1>X / Twitter 关键词研究结果</h1><span>批次 __RUN_ID__ · 原始记录保留 · 研究样本自动去重</span></div><div class='top-actions'><select id='runSwitcher' aria-label='切换历史批次'><option value=''>正在载入历史批次…</option></select><a class='button' href='#dataset'>筛选帖子</a></div></header><main>
<section id='overview' class='kpis' data-od-id='kpi-cards'><div class='card'><span>去重帖子</span><b>__DEDUP_COUNT__</b><em>按 post_id 自动合并</em></div><div class='card'><span>原始关键词命中</span><b>__RAW_COUNT__</b><em>保留交叉命中关系</em></div><div class='card'><span>去重作者</span><b>__AUTHOR_COUNT__</b><em>作者所在地只查一次</em></div><div class='card'><span>所在地有效率</span><b>__KNOWN_RATE__%</b><em>__KNOWN_COUNT__ / __DEDUP_COUNT__ 条</em></div></section>
<div class='notice' id='locationNotice'><strong>所在地状态：</strong>__LOCATION_NOTICE__</div>
<section class='panel' style='margin-top:14px' data-od-id='exports'><h2>一键导出</h2><div class='exports'><a class='button' href='all_exports.zip'>下载全部 ZIP</a><a class='button secondary' href='posts_deduplicated.csv'>去重样本 CSV</a><a class='button secondary' href='posts.csv'>原始命中 CSV</a><a class='button secondary' href='research_dataset.json'>研究样本 JSON</a><a class='button secondary' href='research_coding_template.csv'>内容编码模板</a><button class='button secondary' id='filteredExport'>导出当前筛选</button></div></section>
<section id='dataset' class='panel' style='margin-top:14px' data-od-id='dataset'><h2>帖子筛选与浏览 <span class='count' id='visibleCount'></span></h2><div class='filter-shell'><div class='filter-grid'><label class='filter-field wide'><span>正文或关键词</span><input id='search' placeholder='输入 GPT、模型名称或正文片段'></label><label class='filter-field'><span>作者用户名</span><input id='authorSearch' placeholder='例如 openai'></label><label class='filter-field'><span>所在地状态</span><select id='locationStatus'><option value=''>全部状态</option><option value='known'>已有国家/地区</option><option value='__pending__'>待查询</option><option value='__unavailable__'>X 未公开</option><option value='__failed__'>查询失败</option></select></label><label class='filter-field'><span>国家 / 地区</span><select id='country'><option value=''>全部国家/地区</option>__COUNTRY_OPTIONS__</select></label><label class='filter-field'><span>命中关键词</span><select id='keyword'><option value=''>全部关键词</option>__KEYWORD_OPTIONS__</select></label><label class='filter-field'><span>发帖日期从</span><input id='dateFrom' type='date'></label><label class='filter-field'><span>发帖日期至</span><input id='dateTo' type='date'></label><label class='filter-field'><span>排序</span><select id='sortOrder'><option value='newest'>时间：最新优先</option><option value='oldest'>时间：最早优先</option><option value='author'>作者：A–Z</option><option value='country'>国家：A–Z</option></select></label><label class='filter-field'><span>每页显示</span><select id='pageSize'><option value='25'>25 条</option><option value='50'>50 条</option><option value='100' selected>100 条</option><option value='250'>250 条</option></select></label></div><div class='filter-actions'><span class='hint' id='activeFilterSummary'>当前显示全部去重帖子</span><button class='button secondary' id='reset'>清除全部筛选</button></div></div><div class='filter-summary'><div class='filter-stat'><b id='filteredCount'>0</b>筛选后帖子</div><div class='filter-stat'><b id='filteredAuthors'>0</b>筛选后作者</div><div class='filter-stat'><b id='filteredCountries'>0</b>筛选后国家 / 地区</div></div><div class='table'><table><thead><tr><th>命中关键词</th><th>帖子原文</th><th>发帖时间</th><th>作者</th><th>自填位置</th><th>Account based in</th><th>帖子 ID</th><th>链接</th></tr></thead><tbody id='rows'></tbody></table></div><div class='pager'><button class='button secondary' id='prevPage'>上一页</button><span class='pager-center' id='pageInfo'>第 1 页</span><button class='button secondary' id='nextPage'>下一页</button></div></section>
<section id='coverage' class='panel' style='margin-top:14px'><h2>搜索覆盖诊断</h2><p class='empty'>X 不提供总命中数。cursor 或滚动无新增表示本次搜索看起来已到底，但不等于平台数据库绝对全量；“被预算截断”表示目标条数或页数上限先到了。</p><div class='exports'><span class='button secondary'>搜索片 __SLICE_COUNT__</span><span class='button secondary'>看似已到尽头 __EXHAUSTED_COUNT__</span><span class='button secondary'>被预算截断 __TRUNCATED_COUNT__</span><a class='button secondary' href='coverage_report.csv'>下载覆盖报告</a></div><div class='table' style='max-height:320px;margin-top:12px'><table><thead><tr><th>关键词</th><th>日期片</th><th>帖子数</th><th>翻页方式</th><th>耗时 / 实测速率</th><th>停止原因</th><th>诊断</th></tr></thead><tbody>__COVERAGE_ROWS__</tbody></table></div></section>
<section id='distribution' class='charts' data-od-id='charts'><article class='panel'><h2>账号所在地分布</h2>__COUNTRY_CHART__</article><article class='panel'><h2>关键词命中分布</h2>__KEYWORD_CHART__</article></section><section class='panel' style='margin-top:14px'><h2>帖子时间分布（最近 14 个有数据日期）</h2>__TIMELINE_CHART__</section>
<section class='panel' style='margin-top:14px'><h2>高频作者</h2><table><thead><tr><th>作者</th><th>帖子数</th><th>占比</th></tr></thead><tbody>__AUTHOR_ROWS__</tbody></table><p class='empty'>分组明细同时输出为 country_summary.csv、keyword_summary.csv 和 author_summary.csv。</p></section>
</main></div></div><script>
let dataset=__DATASET__,filtered=[];const runId=__RUN_ID_JSON__,totalExpected=__DEDUP_COUNT__;let page=1,pageSize=100;
const byId=id=>document.getElementById(id),q=byId('search'),authorSearch=byId('authorSearch'),locationStatus=byId('locationStatus'),country=byId('country'),keyword=byId('keyword'),dateFrom=byId('dateFrom'),dateTo=byId('dateTo'),sortOrder=byId('sortOrder'),pageSizeControl=byId('pageSize'),rows=byId('rows'),visible=byId('visibleCount');
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function locationKey(r){if(r.account_based_in)return String(r.account_based_in).toLowerCase();if(r.account_lookup_status==='unknown')return'__unavailable__';if(r.account_lookup_status==='failed')return'__failed__';return'__pending__'}
function locationLabel(r){if(r.account_based_in)return String(r.account_based_in);if(r.account_lookup_status==='unknown')return'X 未公开';if(r.account_lookup_status==='failed')return'查询失败';return'待查询'}
function render(){const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(Math.max(1,page),pages);const start=(page-1)*pageSize;rows.innerHTML=filtered.slice(start,start+pageSize).map(r=>`<tr data-country="${esc(locationKey(r))}" data-keywords="${esc(String(r.matched_keywords||'').toLowerCase())}" data-date="${esc(String(r.posted_at||'').slice(0,10))}"><td>${esc(r.matched_keywords)}</td><td class="post">${esc(r.post_text)}</td><td>${esc(r.posted_at)}</td><td>@${esc(r.author_username)}</td><td>${esc(r.profile_location||'—')}</td><td>${esc(locationLabel(r))}</td><td>${esc(r.post_id)}</td><td><a class="plain" target="_blank" rel="noopener" href="${esc(r.post_url)}">打开</a></td></tr>`).join('');visible.textContent=`筛选结果 ${filtered.length} / ${dataset.length} 条`;byId('filteredCount').textContent=filtered.length;byId('filteredAuthors').textContent=new Set(filtered.map(r=>String(r.author_username||'').toLowerCase()).filter(Boolean)).size;byId('filteredCountries').textContent=new Set(filtered.filter(r=>r.account_based_in).map(r=>String(r.account_based_in).toLowerCase())).size;byId('pageInfo').textContent=`第 ${page} / ${pages} 页 · 每页 ${pageSize} 条`;byId('prevPage').disabled=page<=1;byId('nextPage').disabled=page>=pages}
function apply(){const term=q.value.trim().toLowerCase(),author=authorSearch.value.trim().replace(/^@/,'').toLowerCase(),status=locationStatus.value,c=country.value,k=keyword.value,from=dateFrom.value,to=dateTo.value;pageSize=Number(pageSizeControl.value)||100;filtered=dataset.filter(r=>{const d=String(r.posted_at||'').slice(0,10),hay=[r.post_text,r.matched_keywords].join(' ').toLowerCase(),authorName=String(r.author_username||'').toLowerCase(),loc=locationKey(r);return(!term||hay.includes(term))&&(!author||authorName.includes(author))&&(!status||(status==='known'?Boolean(r.account_based_in):loc===status))&&(!c||loc===c)&&(!k||String(r.matched_keywords||'').toLowerCase().split(' | ').includes(k))&&(!from||!d||d>=from)&&(!to||!d||d<=to)});const order=sortOrder.value,cmpText=(a,b)=>String(a||'').localeCompare(String(b||''),'zh-CN',{sensitivity:'base'});filtered.sort((a,b)=>order==='oldest'?cmpText(a.posted_at,b.posted_at):order==='author'?cmpText(a.author_username,b.author_username):order==='country'?cmpText(locationLabel(a),locationLabel(b)):cmpText(b.posted_at,a.posted_at));const active=[term,author,status,c,k,from,to].filter(Boolean).length;byId('activeFilterSummary').textContent=active?`已启用 ${active} 个筛选条件；可导出当前结果`:'当前显示全部去重帖子';page=1;render()}
for(const control of[q,authorSearch])control.addEventListener('input',apply);for(const control of[locationStatus,country,keyword,dateFrom,dateTo,sortOrder,pageSizeControl])control.addEventListener('change',apply);byId('reset').onclick=()=>{q.value='';authorSearch.value='';locationStatus.value='';country.value='';keyword.value='';dateFrom.value='';dateTo.value='';sortOrder.value='newest';pageSizeControl.value='100';apply()};byId('prevPage').onclick=()=>{page--;render()};byId('nextPage').onclick=()=>{page++;render()};
function csvCell(v){const s=String(v??'');return '"'+s.replaceAll('"','""')+'"'}byId('filteredExport').onclick=()=>{const fields=['post_id','post_url','post_text','posted_at','author_username','author_user_id','profile_location','account_based_in','matched_keywords','account_lookup_status'];const csv='\ufeff'+[fields.join(','),...filtered.map(r=>fields.map(f=>csvCell(r[f])).join(','))].join('\r\n');const a=document.createElement('a'),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.href=url;a.download='filtered_posts.csv';a.style.display='none';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)};
async function loadRuns(){const select=byId('runSwitcher');try{const response=await fetch('/api/history'),data=await response.json();select.innerHTML=data.runs.map(r=>`<option value="${esc(r.run_id)}" ${r.run_id===runId?'selected':''}>${esc(r.run_id)} · ${esc((r.keywords||[]).join('、')||'无关键词')} · ${r.post_count} 条 · ${r.status==='completed'?'已完成':'未完成'}</option>`).join('');select.onchange=()=>{if(select.value&&select.value!==runId)location.href='/results/'+encodeURIComponent(select.value)+'/index.html'}}catch{select.innerHTML=`<option value="${esc(runId)}">批次 ${esc(runId)}</option>`}}async function init(){if(!dataset.length&&totalExpected){visible.textContent='正在载入帖子数据…';try{const response=await fetch('research_dataset.json');dataset=await response.json()}catch{visible.textContent='帖子数据载入失败，请返回采集工作台重试';return}}loadRuns();apply()}init();
</script></body></html>"""
    replacements = {
        "__UPDATED__": html.escape(now_iso()),
        "__DEDUP_COUNT__": str(len(deduped)), "__RAW_COUNT__": str(len(posts)),
        "__AUTHOR_COUNT__": str(len(authors)), "__KNOWN_RATE__": str(known_rate),
        "__KNOWN_COUNT__": str(known), "__COUNTRY_CHART__": bar_chart_svg(countries, "country", "账号所在地分布"),
        "__SLICE_COUNT__": str(len(reliable_search_keys(coverage))), "__EXHAUSTED_COUNT__": str(exhausted_count), "__TRUNCATED_COUNT__": str(truncated_count), "__COVERAGE_ROWS__": coverage_rows,
        "__KEYWORD_CHART__": bar_chart_svg(keywords, "keyword", "关键词命中分布"),
        "__TIMELINE_CHART__": timeline_chart_svg(dates),
        "__COUNTRY_OPTIONS__": country_options, "__KEYWORD_OPTIONS__": keyword_options,
        "__AUTHOR_ROWS__": author_rows, "__DATASET__": embedded,
        "__RUN_ID_JSON__": json.dumps(run_id, ensure_ascii=False), "__RUN_ID__": html.escape(run_id),
        "__LOCATION_NOTICE__": html.escape(location_notice),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template.replace('cursor 或滚动无新增表示本次搜索看起来已到底', '仅原始响应结束证据或连续成功空尾部核验才标记本次查询到底；重复/无新增/超时不算到底').replace('看似已到尽头 __EXHAUSTED_COUNT__','已核验到底 __EXHAUSTED_COUNT__')


def _status_payload(payload: dict[str, object]) -> dict[str, object]:
    # Cache is restricted to live status, never final acceptance/resume checks.
    with evidence_cache_scope(_STATUS_EVIDENCE_CACHE), measure("status_build"):
        return _build_status_payload(payload)


def _build_status_payload(payload: dict[str, object], *, repair_queue=None, profile_queue=None) -> dict[str, object]:
    coverage = list(payload.get("search_runs", []))
    completed = reliable_search_keys(coverage)
    if repair_queue is None:
        repair_queue = search_repair_queue(coverage, str(payload.get("repair_policy", "strict")))
    authors = payload.get("authors", {}) if isinstance(payload.get("authors"), dict) else {}
    if profile_queue is None:
        profile_queue = profile_location_queue(authors)
    return {
        "keyword_count": len(payload.get("requested_keywords", [])),
        "completed_keywords": len(completed),
        "post_count": len(payload.get("posts", [])),
        "resolved_authors": sum(
            isinstance(author, dict) and author.get("status") != "pending"
            for author in payload.get("authors", {}).values()
        ) if isinstance(payload.get("authors"), dict) else 0,
        "search_slice_count": len(completed),
        "search_attempt_count": len(coverage),
        "repair_queue_count": len(repair_queue),
        "profile_location_count": sum(bool(author.get("profile_location")) for author in authors.values() if isinstance(author, dict)),
        "profile_location_backlog_count": len(profile_queue),
        "apparently_exhausted_slices": sum(search_end_verified(run) for run in coverage if isinstance(run,dict)),
        "verified_search_end_slices": len({search_run_key(run) for run in coverage if isinstance(run,dict) and search_end_verified(run)}),
        "validated_empty_tail_slices": len({search_run_key(run) for run in coverage if isinstance(run,dict) and run.get('stop_reason')=='validated_empty_tail' and validated_empty_tail(run)}),
        "guard_hold": payload.get("guard_hold"),
        "active_phase": payload.get("active_phase", ""),
        "truncated_slices": sum(run.get("coverage_status") == "truncated" for run in coverage if isinstance(run, dict)),
        "finalized": bool(payload.get("finalized", False)),
        "updated_at": now_iso(),
    }


def save_checkpoint(output_dir: Path, payload: dict[str, object]) -> None:
    """Persist resumable state without rebuilding every research export."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(output_dir / "raw.json", payload)
    with evidence_cache_scope(_STATUS_EVIDENCE_CACHE), measure("status_build"):
        repairs = search_repair_queue(payload.get("search_runs", []), str(payload.get("repair_policy", "strict")))
        profiles = profile_location_queue(payload.get("authors", {}))
        status = _build_status_payload(payload, repair_queue=repairs, profile_queue=profiles)
    _write_json_atomic(output_dir / "status.json", status)
    _write_json_atomic(output_dir / "repair_queue.json", repairs)
    _write_json_atomic(output_dir / "profile_location_queue.json", profiles)


@timed("export")
def save_outputs(output_dir: Path, payload: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    posts = list(payload.get("posts", []))
    deduped = deduplicate_posts(posts)
    countries = count_rows([str(row.get("account_based_in", "")) for row in deduped], "country")
    keywords = count_rows([str(row.get("keyword", "")) for row in posts], "keyword")
    authors = count_rows([str(row.get("author_username", "")) for row in deduped], "author")
    dates = count_rows([str(row.get("posted_at", ""))[:10] for row in deduped if row.get("posted_at")], "date")
    dates.sort(key=lambda row: str(row["date"]))
    coverage = list(payload.get("search_runs", []))
    _write_json_atomic(output_dir / "raw.json", payload)
    _write_json_atomic(output_dir / "repair_queue.json", search_repair_queue(coverage, str(payload.get("repair_policy", "strict"))))
    _write_json_atomic(output_dir / "profile_location_queue.json", profile_location_queue(payload.get("authors", {})))
    write_csv(output_dir / "posts.csv", posts)
    write_csv(output_dir / "posts_deduplicated.csv", deduped, DEDUP_FIELDS)
    write_csv(output_dir / "research_coding_template.csv", deduped, CODING_FIELDS)
    write_csv(output_dir / "country_summary.csv", countries, ["country", "count", "percent"])
    write_csv(output_dir / "keyword_summary.csv", keywords, ["keyword", "count", "percent"])
    write_csv(output_dir / "author_summary.csv", authors, ["author", "count", "percent"])
    write_csv(output_dir / "posted_date_summary.csv", dates, ["date", "count", "percent"])
    classified=[{**r,**classify_search_run(r)} for r in coverage]
    write_csv(output_dir / "coverage_report.csv", classified, ["keyword", "keyword_index", "query", "slice_start", "slice_end", "count", "search_engine", "request_transport", "request_method", "operation_name", "endpoint_path", "json_pages_used", "scrolls_used", "pagination_trigger_retries", "last_cursor", "started_at", "elapsed_seconds", "posts_per_minute", "stop_reason", "coverage_status", "state_code", "state_label", "state_category", "empty_tail_candidate", "search_end_status", "search_end_verified", "search_end_scope", "search_end_evidence_file", "platform_all_posts_proven", "rate_limit_limit", "rate_limit_remaining", "rate_limit_reset", "completed_at"])
    (output_dir / "research_dataset.json").write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "index.html").write_text(report_html(posts, payload, output_dir.name), encoding="utf-8")
    _write_json_atomic(output_dir / "status.json", _status_payload(payload))
    export_names = [
        "posts.csv", "posts_deduplicated.csv", "research_coding_template.csv",
        "country_summary.csv", "keyword_summary.csv", "author_summary.csv", "posted_date_summary.csv", "coverage_report.csv",
        "research_dataset.json", "raw.json", "status.json", "repair_queue.json", "profile_location_queue.json", "index.html",
    ]
    with zipfile.ZipFile(output_dir / "all_exports.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in export_names:
            path = output_dir / name
            if path.exists():
                archive.write(path, arcname=name)


def collect_search(page, keyword: str, keyword_index: int, limit: int, max_scrolls: int, render_wait_ms: int, output_dir: Path, start_date: date | None = None, end_date: date | None = None, search_type: str = "live", no_growth_stop: int = 5, scroll_delay_min: float = 3, scroll_delay_max: float = 6, timezone_offset: float = 8) -> tuple[list[dict[str, object]], dict[str, object]]:
    query = build_search_query(keyword, start_date, end_date, timezone_offset)
    suffix = "&f=live" if search_type == "live" else ""
    url = f"https://x.com/search?q={quote(query)}&src=typed_query{suffix}"
    page.goto(url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(render_wait_ms)
    ready_state = wait_for_search_ready(page, 90)
    body = page.locator("body").inner_text(timeout=15_000)
    if "Log in to X" in body and not page.locator('article[data-testid="tweet"]').count():
        raise RuntimeError("搜索页要求重新登录")
    if "Rate limit exceeded" in body:
        raise RuntimeError("搜索页触发限流")
    screenshot_dir = output_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    slice_label = f"{start_date.isoformat() if start_date else 'open'}_{end_date.isoformat() if end_date else 'open'}"
    page.screenshot(path=str(screenshot_dir / f"{keyword_index:03d}_{safe_name(keyword)}_{slice_label}.png"), full_page=False)

    if ready_state == "empty":
        return [], {
            "query": query, "slice_start": start_date.isoformat() if start_date else "",
            "slice_end": end_date.isoformat() if end_date else "", "count": 0,
            "scrolls_used": 0, "stop_reason": "explicit_no_results",
            "coverage_status": "apparently_exhausted",
        }

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    no_growth = 0
    stop_reason = "max_scrolls"
    scrolls_used = 0
    for scroll_index in range(max_scrolls + 1):
        scrolls_used = scroll_index + 1
        if not page.locator('article[data-testid="tweet"]').count():
            wait_for_search_ready(page, 30)
        added = 0
        for row in extract_visible_posts(page, keyword, keyword_index):
            if not post_in_date_range(str(row.get("posted_at", "")), start_date, end_date, timezone_offset):
                continue
            post_id = str(row["post_id"])
            if post_id in seen:
                continue
            seen.add(post_id)
            row["rank"] = len(rows) + 1
            rows.append(row)
            added += 1
            if limit > 0 and len(rows) >= limit:
                break
        target_label = str(limit) if limit > 0 else "不限"
        print(f"  滚动 {scroll_index + 1}/{max_scrolls + 1}：新增 {added}，累计 {len(rows)}/{target_label}", flush=True)
        if limit > 0 and len(rows) >= limit:
            stop_reason = "target_reached"
            break
        no_growth = no_growth + 1 if added == 0 else 0
        if no_growth >= no_growth_stop:
            stop_reason = "no_new_results"
            break
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(int(random.uniform(scroll_delay_min, scroll_delay_max) * 1000))
    selected = rows[:limit] if limit > 0 else rows
    coverage = {
        "query": query,
        "slice_start": start_date.isoformat() if start_date else "",
        "slice_end": end_date.isoformat() if end_date else "",
        "count": len(selected),
        "scrolls_used": scrolls_used,
        "stop_reason": stop_reason,
        "coverage_status": "needs_retry" if stop_reason == "no_new_results" else "truncated",
        "date_timezone": f"UTC{timezone_offset:+g}",
    }
    return selected, coverage


def _replay_search(page, template: dict[str, object], cursor: str) -> tuple[dict[str, object], dict[str, str]]:
    url = _search_url_with_cursor(str(template["url"]), cursor)
    replay = page.evaluate(
        """async ({url, method, headers, body}) => {
            const response = await fetch(url, {
                method, headers, body: method === 'GET' ? undefined : body,
                credentials: 'include'
            });
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            return {status: response.status, ok: response.ok, payload, headers: {
                'retry-after': response.headers.get('retry-after') || '',
                'x-rate-limit-limit': response.headers.get('x-rate-limit-limit') || '',
                'x-rate-limit-reset': response.headers.get('x-rate-limit-reset') || '',
                'x-rate-limit-remaining': response.headers.get('x-rate-limit-remaining') || ''
            }};
        }""",
        {"url": url, "method": template["method"], "headers": template["headers"], "body": template.get("body")},
    )
    status = int(replay.get("status", 0))
    headers = replay.get("headers", {}) if isinstance(replay.get("headers"), dict) else {}
    if status == 429:
        raise SearchRateLimitError(_retry_after_seconds(headers))
    if status in {401, 403}:
        raise RuntimeError(f"SearchTimeline HTTP {status}；登录会话可能失效")
    if not replay.get("ok"):
        raise RuntimeError(f"SearchTimeline HTTP {status or 'unknown'}")
    payload = replay.get("payload", {})
    if not isinstance(payload, dict):
        raise RuntimeError("SearchTimeline 未返回 JSON")
    return payload, headers


def checked_search_response(response, evidence_dir=None, expected_query=None, expected_cursor=None):
    """Keep authoritative reset/backoff attached even when JSON parsing fails."""
    headers = _response_headers(response)
    if response.status != 200 and evidence_dir is not None:
        evidence_dir.mkdir(parents=True,exist_ok=True)
        body=getattr(response,'_failure_body',None)
        if body is None:
            try: body=response.json()
            except Exception as exc: body={'body_unavailable':str(exc)}
        failure_file=evidence_dir/f'http_{response.status}_{time.time_ns()}.json'
        _write_json_atomic(failure_file,{'received_at':now_iso(),'http_status':response.status,
            'query':_search_raw_query(response.url,response.request.post_data),
            'expected_query':expected_query,'request_cursor':_search_request_cursor(response.url,response.request.post_data),
            'expected_cursor':expected_cursor,'endpoint_path':_graphql_operation(response.url)[1],
            'rate_limit_headers':headers,'body':body})
        print(f'[错误响应留证] HTTP {response.status} 已保存 {failure_file.name}；不是搜索结束。',flush=True)
    if response.status == 429:
        raise SearchRateLimitError(_retry_after_seconds(headers))
    try:
        if response.status != 200:
            raise RuntimeError(f"SearchTimeline HTTP {response.status}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("SearchTimeline 响应不是 JSON 对象")
        validate_search_payload(payload)
        return payload
    except Exception as exc:
        exc.search_retry_after = _retry_after_seconds(headers) or 0
        raise


def collect_search_graphql(page, keyword: str, keyword_index: int, limit: int, max_pages: int, output_dir: Path, start_date: date | None = None, end_date: date | None = None, search_type: str = "live", no_growth_stop: int = 5, request_delay_min: float = 2, request_delay_max: float = 4, timezone_offset: float = 8, pagination_trigger_attempts: int = 3, pagination_trigger_timeout_ms: int = 15_000, checkpoint=None, resume_record=None, seed_rows=None) -> tuple[list[dict[str, object]], dict[str, object]]:
    timings = current_timings() or StageTimings()
    with timing_scope(timings):
        try:
            with measure("search_total"):
                return _collect_search_graphql_listening(page, keyword, keyword_index, limit, max_pages, output_dir, start_date, end_date, search_type, no_growth_stop, request_delay_min, request_delay_max, timezone_offset, pagination_trigger_attempts, pagination_trigger_timeout_ms, checkpoint, resume_record, seed_rows)
        finally:
            _write_json_atomic(output_dir / "timings.json", timings.snapshot())


def _collect_search_graphql_listening(page, keyword, keyword_index, limit, max_pages, output_dir, start_date, end_date, search_type, no_growth_stop, request_delay_min, request_delay_max, timezone_offset, pagination_trigger_attempts, pagination_trigger_timeout_ms, checkpoint, resume_record, seed_rows):
    query = build_search_query(keyword, start_date, end_date, timezone_offset)
    buffer = []
    def capture(response):
        if _search_response_matches(response, query):
            buffer.append(_BufferedSearchResponse(response))
    listening = hasattr(page, 'on')
    if listening:
        page.on('response', capture)
    try:
        return _collect_search_graphql_impl(page, keyword, keyword_index, limit, max_pages, output_dir, start_date, end_date, search_type, no_growth_stop, request_delay_min, request_delay_max, timezone_offset, pagination_trigger_attempts, pagination_trigger_timeout_ms, checkpoint, buffer if listening else None, resume_record, seed_rows)
    finally:
        if listening:
            page.remove_listener('response', capture)


def collect_search_http(transport, keyword, keyword_index, limit, max_pages, output_dir, start_date=None, end_date=None, *, search_type="live", no_growth_stop=5, request_delay_min=2, request_delay_max=4, timezone_offset=8, checkpoint=None, resume_record=None, seed_rows=None):
    """Isolated experimental API; caller supplies fresh signing and its own client.

    Not exposed by the production UI/CLI. Same parser, end policy, checkpoints
    and pacing as the browser route. This is not a real-session validation.
    """
    if search_type != 'live':
        raise ValueError('Experimental HTTP validation currently requires Latest')
    if request_delay_min < 2 or request_delay_max < request_delay_min or not 1 <= max_pages <= 1000:
        raise ValueError('HTTP pilot requires delay >=2 seconds and page budget 1..1000')
    query = build_search_query(keyword, start_date, end_date, timezone_offset)
    if transport.query != query:
        raise ValueError('Experimental HTTP query mismatch')
    timings = current_timings() or StageTimings()
    with timing_scope(timings):
        try:
            with measure('search_total'):
                return _collect_search_graphql_impl(None, keyword, keyword_index, limit, max_pages, output_dir, start_date, end_date, search_type, no_growth_stop, request_delay_min, request_delay_max, timezone_offset, checkpoint=checkpoint, resume_record=resume_record, seed_rows=seed_rows, transport=transport)
        finally:
            _write_json_atomic(output_dir / 'timings.json', timings.snapshot())


def _collect_search_graphql_impl(page, keyword: str, keyword_index: int, limit: int, max_pages: int, output_dir: Path, start_date: date | None = None, end_date: date | None = None, search_type: str = "live", no_growth_stop: int = 5, request_delay_min: float = 2, request_delay_max: float = 4, timezone_offset: float = 8, pagination_trigger_attempts: int = 3, pagination_trigger_timeout_ms: int = 15_000, checkpoint=None, buffered_responses=None, resume_record=None, seed_rows=None, transport=None) -> tuple[list[dict[str, object]], dict[str, object]]:
    query = build_search_query(keyword, start_date, end_date, timezone_offset)
    offset = 0
    resume_cursor = ''
    if resume_record is not None:
        if resume_record.get('query') != query or resume_record.get('search_type', search_type) != search_type:
            raise ValueError('续采查询/排序不兼容；禁止从首页重跑')
        if resume_record.get('guard_hold') or int(resume_record.get('date_rejected_count',0) or 0):
            raise ValueError('续采存在质量保护或日期排除；禁止启动')
        resume_cursor = str(resume_record.get('last_cursor') or '')
        offset = int(resume_record.get('json_pages_used',0) or 0)
        if not resume_cursor or not 0 < offset < max_pages or not seed_rows:
            raise ValueError('缺失续采游标/已保存样本或超出原页数预算；禁止从头重跑')
        if any(not post_in_date_range(str(row.get('posted_at','')),start_date,end_date,timezone_offset) for row in seed_rows):
            raise ValueError('续采基线包含区间外帖子')
    suffix = "&f=live" if search_type == "live" else ""
    url = f"https://x.com/search?q={quote(query)}&src=typed_query{suffix}"
    try:
        if transport is None:
            with measure("request_wait"), page.expect_response(lambda r: _search_response_matches(r, query, ""), timeout=90_000) as pending:
                page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            response = _take_buffered_response(pending.value, buffered_responses)
        else:
            response = transport.fetch('')
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("未捕获到 SearchTimeline JSON 响应") from exc
    if response.status == 429:
        raise SearchRateLimitError(_retry_after_seconds(_response_headers(response)))
    first_payload = checked_search_response(response,output_dir/'search_failure_evidence',query,'')
    if not isinstance(first_payload, dict):
        raise RuntimeError("SearchTimeline 首次响应不是 JSON 对象")
    validate_search_payload(first_payload)
    first_rows, first_cursor = parse_search_timeline(first_payload, keyword, keyword_index)
    if resume_record is not None:
        # The first response is only a fresh-account/date preflight. It is not
        # counted or added to the dataset. Resume the exact persisted cursor.
        if not first_rows or any(not post_in_date_range(str(r.get('posted_at','')),start_date,end_date,timezone_offset) for r in first_rows):
            raise RuntimeError('续采首页健康/日期核验失败；不回退首页采集')
        state = _rate_limit_state(_response_headers(response))
        pause = random.uniform(request_delay_min,request_delay_max)
        if state.get('remaining',999) <= 5 and state.get('reset',0) > time.time():
            pause = max(pause,state['reset']-time.time()+random.uniform(5,20))
        measured_sleep(pause)
        response = _fresh_browser_cursor_response(page,query,resume_cursor,buffered_responses) if transport is None else transport.fetch(resume_cursor)
        first_payload = checked_search_response(response,output_dir/'search_failure_evidence',query,resume_cursor)
        print(f'[精确游标续采] 从已保存页 {offset} 的下一页开始；首页仅健康检查，不入库。',flush=True)
    elif not first_rows and first_cursor:
        # An empty page with a continuation cursor is not proof of an empty day.
        # Reinitialise this same page once, respecting the observed reset header.
        state = _rate_limit_state(_response_headers(response))
        reset = state.get("reset", 0)
        pause = random.uniform(request_delay_min, request_delay_max)
        if state.get("remaining", 999) <= 5 and reset > time.time():
            pause = max(pause, reset - time.time() + random.uniform(5, 20))
        print(f"[空响应保护] 首页零条但含游标；等待 {pause / 60:.1f} 分钟后重新加载同一查询，不推进日期。", flush=True)
        measured_sleep(pause)
        if transport is None:
            with measure("request_wait"), page.expect_response(lambda r: _search_response_matches(r, query, ""), timeout=90_000) as refreshed:
                page.reload(wait_until="domcontentloaded", timeout=90_000)
            response = _take_buffered_response(refreshed.value, buffered_responses)
        else:
            response = transport.fetch('')
        if response.status == 429:
            raise SearchRateLimitError(_retry_after_seconds(_response_headers(response)))
        first_payload = checked_search_response(response,output_dir/'search_failure_evidence',query,'')
        validate_search_payload(first_payload)
        first_rows, first_cursor = parse_search_timeline(first_payload, keyword, keyword_index)
        if not first_rows and first_cursor:
            raise RuntimeError("SearchTimeline 连续空响应且含游标；保留原日期，停止空跑，需检查页面/响应")
    template = _search_template(response)
    operation_name, endpoint_path = _graphql_operation(str(template["url"]))
    rows: list[dict[str, object]] = list(seed_rows or [])
    baseline_count = len(rows)
    seen: set[str] = {str(r.get('post_id','')) for r in rows if r.get('post_id')}
    seen_cursors: set[str] = set()
    payload = first_payload
    stop_reason = "max_pages"
    pages_used = 0
    no_growth = 0
    rate_limit: dict[str, float] = _rate_limit_state(_response_headers(response))
    pagination_trigger_retries = 0
    last_cursor = ""
    target_label = str(limit) if limit > 0 else "不限"

    parsed_total = 0
    date_rejected_total = 0
    duplicate_total = 0
    returned_date_examples: list[str] = []
    empty_tail_responses = []
    consumed_response_ids = set()
    consumed_response_refs = []
    response_expected_cursor = resume_cursor
    evidence_dir = output_dir / 'search_end_evidence' / (hashlib.sha256(query.encode()).hexdigest()[:12] + '_' + str(time.time_ns()))

    response_manifest = []
    for page_index in range(max_pages-offset):
        pages_used = offset + page_index + 1
        if page_index == 0 and resume_record is None:
            parsed, cursor = first_rows, first_cursor
        else:
            parsed, cursor = parse_search_timeline(payload, keyword, keyword_index)
        response_identity = id(getattr(response, 'original', response))
        association_valid = _search_response_matches(response, query, response_expected_cursor)
        page_archive = None
        if resume_record is not None:
            if not association_valid:
                raise RuntimeError('续采响应查询/游标不匹配；禁止推进检查点')
            evidence_dir.mkdir(parents=True,exist_ok=True)
            archive=evidence_dir/f'continuation_page_{pages_used:04d}.json'
            archive_digest = _write_json_atomic(archive,payload,return_digest=True)
            page_archive = {'page':pages_used,'query':query,'http_status':response.status,
                'request_cursor':response_expected_cursor,'next_cursor':cursor or '',
                'parsed_post_ids':[r.get('post_id') for r in parsed],
                'response_file':str(archive.resolve()),'sha256':archive_digest}
            response_manifest.append(page_archive)
            _write_json_atomic(evidence_dir/'responses_manifest.json',response_manifest)
        if rows and not parsed and cursor and _cursor_only_empty_payload(payload) and response.status == 200 and association_valid and response_identity not in consumed_response_ids:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            response_file = Path(page_archive['response_file']) if page_archive else evidence_dir / f'page_{pages_used:04d}.json'
            response_digest = page_archive['sha256'] if page_archive else _write_json_atomic(response_file, payload, return_digest=True)
            empty_tail_responses.append({'page':pages_used, 'http_status':200, 'query':query,
                'request_cursor':_search_request_cursor(response.url,response.request.post_data),
                'request_cursor_matches':True, 'next_cursor':cursor, 'parsed_count':0,
                'received_at':now_iso(), 'response_file':str(response_file.resolve()),
                'sha256':response_digest})
        else:
            empty_tail_responses = []
        consumed_response_ids.add(response_identity)
        consumed_response_refs.append(getattr(response, 'original', response))
        parsed_total += len(parsed)
        added = 0
        date_rejected = 0
        with measure("date_filter_dedup"):
            for row in parsed:
                if not post_in_date_range(str(row.get("posted_at", "")), start_date, end_date, timezone_offset):
                    date_rejected += 1
                    date_rejected_total += 1
                    stamp = str(row.get("posted_at", ""))
                    if stamp not in returned_date_examples and len(returned_date_examples) < 5:
                        returned_date_examples.append(stamp)
                    continue
                post_id = str(row.get("post_id", ""))
                if not post_id or post_id in seen:
                    duplicate_total += 1
                    continue
                seen.add(post_id)
                row["rank"] = len(rows) + 1
                rows.append(row)
                added += 1
                if limit > 0 and len(rows) >= limit:
                    break
        print(f"  JSON页 {pages_used}/{max_pages}：新增 {added}，累计 {len(rows)}/{target_label}", flush=True)
        if checkpoint is not None:
            checkpoint(rows, {"query": query, "slice_start": start_date.isoformat() if start_date else "", "slice_end": end_date.isoformat() if end_date else "", "count": len(rows), "json_pages_used": pages_used, "last_cursor": cursor or "", "parsed_post_count": parsed_total, "date_rejected_count": date_rejected_total, "duplicate_or_missing_id_count": duplicate_total, "returned_date_examples": list(returned_date_examples), "search_engine": "graphql", "stop_reason": "in_progress", "coverage_status": "needs_retry", "search_end_evidence":{"policy":EMPTY_TAIL_POLICY,"responses":list(empty_tail_responses)}})
        print(f"  [响应诊断] 解析 {len(parsed)}，日期排除 {date_rejected}，{'已落盘' if checkpoint is not None else '内存候选'} {len(rows)}。", flush=True)
        if limit > 0 and len(rows) >= limit:
            stop_reason = "target_reached"
            break
        if len(empty_tail_responses) >= EMPTY_TAIL_REQUIRED_RESPONSES and date_rejected_total == 0:
            candidate = {'query':query,'count':len(rows),'date_rejected_count':date_rejected_total,
                         'search_end_evidence':{'policy':EMPTY_TAIL_POLICY,'responses':empty_tail_responses}}
            if not validated_empty_tail(candidate):
                raise RuntimeError('空尾部原始证据核验失败；禁止标记搜索结束')
            stop_reason = 'validated_empty_tail'
            last_cursor = cursor
            print('[搜索结束核验] 连续3个成功且查询/游标匹配的游标空页；记为已核验空尾部，不代表平台全量。', flush=True)
            break
        no_growth = no_growth + 1 if added == 0 else 0
        if no_growth >= no_growth_stop:
            stop_reason = "date_filter_mismatch" if not rows and parsed_total and date_rejected_total == parsed_total else "no_new_results"
            break
        if not cursor:
            stop_reason = "date_filter_mismatch" if not rows and parsed_total and date_rejected_total == parsed_total else "cursor_exhausted"
            break
        if cursor in seen_cursors and not empty_tail_responses:
            stop_reason = "date_filter_mismatch" if not rows and parsed_total and date_rejected_total == parsed_total else "repeated_cursor"
            break
        seen_cursors.add(cursor)
        last_cursor = cursor
        if pages_used >= max_pages:
            # A bounded verification must not issue one more page request that
            # the loop cannot parse or persist.
            stop_reason = "max_pages"
            break
        remaining = rate_limit.get("remaining")
        reset = rate_limit.get("reset")
        if isinstance(remaining, (int, float)) and remaining <= 5 and isinstance(reset, (int, float)) and reset > time.time():
            pause = max(5.0, reset - time.time()) + random.uniform(5, 20)
            print(f"  [搜索响应头节奏] 当前额度剩余 {int(remaining)}，按 reset 暂停 {pause / 60:.1f} 分钟。", flush=True)
            measured_sleep(pause, "rate_limit_sleep")
        else:
            measured_sleep(random.uniform(request_delay_min, request_delay_max))
        if transport is None:
            next_response, trigger_attempt = _next_browser_signed_search_response(
                page, query, pagination_trigger_attempts, pagination_trigger_timeout_ms, expected_cursor=cursor, buffered_responses=buffered_responses,
            )
        else:
            next_response, trigger_attempt = transport.fetch(cursor), 1
        pagination_trigger_retries += max(0, trigger_attempt - 1)
        if transport is None and next_response is None and (empty_tail_responses or resume_record is not None):
            print('[游标核验] 网页触发无响应；使用新浏览器签名核验同一游标，不把超时当结束或回退首页。', flush=True)
            next_response = _fresh_browser_cursor_response(page, query, cursor, buffered_responses)
        if next_response is None:
            stop_reason = "pagination_not_triggered"
            print(
                f"  [分页触发失败] 已按当前查询重试 {pagination_trigger_attempts} 次，"
                "不是断网判定；已保存当前帖子与游标，稍后进入修复队列。",
                flush=True,
            )
            break
        if next_response.status == 429:
            raise SearchRateLimitError(_retry_after_seconds(_response_headers(next_response)))
        association = "当前日期查询及请求游标已关联" if next_response.status == 200 else "当前日期查询匹配，错误交由保护/退避处理"
        print(f"  [分页诊断] 收到 HTTP {next_response.status}，{association}；触发次数 {trigger_attempt}。", flush=True)
        payload = checked_search_response(next_response,output_dir/'search_failure_evidence',query,cursor)
        response = next_response
        response_expected_cursor = cursor
        if not isinstance(payload, dict):
            raise RuntimeError("SearchTimeline 翻页响应不是 JSON 对象")
        validate_search_payload(payload)
        rate_limit = _rate_limit_state(_response_headers(next_response))

    selected = rows[:limit] if limit > 0 else rows
    coverage = {
        "query": query,
        "slice_start": start_date.isoformat() if start_date else "",
        "slice_end": end_date.isoformat() if end_date else "",
        "count": len(selected),
        "scrolls_used": 0,
        "json_pages_used": pages_used,
        "pagination_trigger_retries": pagination_trigger_retries,
        "last_cursor": last_cursor,
        "stop_reason": stop_reason,
        "coverage_status": (
            "search_tail_verified"
            if stop_reason == 'validated_empty_tail'
            else "needs_retry"
            if stop_reason in RETRYABLE_SEARCH_STOP_REASONS or stop_reason in {'no_new_results','repeated_cursor'}
            else "apparently_exhausted"
            if stop_reason in {"cursor_exhausted", "no_new_results", "repeated_cursor"}
            else "truncated"
        ),
        "date_timezone": f"UTC{timezone_offset:+g}",
        "search_engine": "graphql",
        "search_type": search_type,
        "parsed_post_count": parsed_total,
        "date_rejected_count": date_rejected_total,
        "duplicate_or_missing_id_count": duplicate_total,
        "returned_date_examples": returned_date_examples,
        "request_transport": "browser_signed_network_json" if transport is None else "experimental_direct_http_json",
        "request_method": str(template.get("method", "GET")),
        "operation_name": operation_name,
        "endpoint_path": endpoint_path,
        "rate_limit_limit": rate_limit.get("limit", ""),
        "rate_limit_remaining": rate_limit.get("remaining", ""),
        "rate_limit_reset": rate_limit.get("reset", ""),
        "search_end_status": 'validated_empty_tail' if stop_reason == 'validated_empty_tail' else 'cursor_exhausted' if stop_reason == 'cursor_exhausted' else 'unverified_stop',
        "search_end_verified": stop_reason in {'validated_empty_tail','cursor_exhausted'},
        "search_end_scope": 'this_query_this_account_not_platform_all_posts',
        "search_end_evidence": {'policy':EMPTY_TAIL_POLICY,'required_empty_responses':EMPTY_TAIL_REQUIRED_RESPONSES,'responses':empty_tail_responses},
        "platform_all_posts_proven": False,
    }
    if resume_record is not None:
        coverage.update(resumed_from_page=offset,resumed_from_cursor=resume_cursor,
                        baseline_saved_posts=baseline_count,continuation_new_unique_posts=len(selected)-baseline_count,
                        count_basis='baseline_date_union_plus_continuation_unique',
                        prior_chain_verified=False,natural_day_complete_proven=False,
                        continuation_response_manifest=str((evidence_dir/'responses_manifest.json').resolve()))
    if stop_reason=='cursor_exhausted':
        evidence_dir.mkdir(parents=True,exist_ok=True)
        response_file=Path(page_archive['response_file']) if page_archive else evidence_dir/'final_response.json'
        response_digest=page_archive['sha256'] if page_archive else _write_json_atomic(response_file,payload,return_digest=True)
        coverage['search_end_evidence']={'policy':'response_end_v1','responses':[{
            'http_status':response.status,'query':query,
            'request_cursor':_search_request_cursor(response.url,response.request.post_data),
            'request_cursor_matches':association_valid,'next_cursor':cursor or '',
            'parsed_count':len(parsed),'received_at':now_iso(),
            'response_file':str(response_file.resolve()),'sha256':response_digest}]}
    derived=classify_search_run(coverage)
    coverage.update(derived)
    coverage['search_end_status']=derived['state_code']
    if stop_reason=='cursor_exhausted' and not derived['search_end_verified']:
        coverage['coverage_status']='needs_retry'
    if empty_tail_responses or stop_reason=='cursor_exhausted':
        verification_path = evidence_dir / 'verification.json'
        _write_json_atomic(verification_path, coverage)
        coverage['search_end_evidence_file'] = str(verification_path.resolve())
    return selected, coverage


def _direct_about_url(url: str, username: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    changed = False
    for key, values in query.items():
        rewritten = []
        for value in values:
            if key == "variables":
                variables = json.loads(value)
                for candidate in ("screen_name", "screenName", "username"):
                    if candidate in variables:
                        variables[candidate] = username
                        changed = True
                value = json.dumps(variables, separators=(",", ":"))
            rewritten.append(value)
        query[key] = rewritten
    if not changed:
        raise RuntimeError("AboutAccountQuery 中未找到用户名参数")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def _author_from_about(username: str, payload: dict[str, object]) -> dict[str, object]:
    result = about_result(payload)
    if not result:
        raise RuntimeError("AboutAccountQuery 未返回用户结果")
    about_profile = result.get("about_profile", {})
    about_profile = about_profile if isinstance(about_profile, dict) else {}
    return {
        "username": username,
        "user_id": result.get("rest_id", ""),
        "account_based_in": about_profile.get("account_based_in", "") or "",
        "status": "success" if about_profile.get("account_based_in") else "unknown",
        "error": "",
        "collected_at": now_iso(),
    }


def _direct_profile_url(url: str, username: str, user_id: str = "") -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    values = query.get("variables")
    if not values:
        raise RuntimeError("用户资料请求中没有 variables")
    variables = json.loads(values[0])
    if not isinstance(variables, dict):
        raise RuntimeError("用户资料 variables 格式无效")
    changed = False
    for key in ("screen_name", "screenName", "username"):
        if key in variables:
            variables[key] = username
            changed = True
    for key in ("userId", "user_id", "rest_id"):
        if key in variables and user_id:
            variables[key] = user_id
            changed = True
    if not changed:
        raise RuntimeError("用户资料请求中未找到可替换的用户名或用户 ID")
    query["variables"] = [json.dumps(variables, separators=(",", ":"), ensure_ascii=False)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def _profile_author_from_payload(username: str, payload: dict[str, object]) -> dict[str, object]:
    result = _nested_dict(payload, "data", "user", "result")
    if not result:
        raise RuntimeError("UserByScreenName 未返回用户结果")
    if str(result.get("__typename", "")) == "UserUnavailable":
        raise RuntimeError("用户不可用、受限或已停用")
    core = result.get("core") if isinstance(result.get("core"), dict) else {}
    legacy = result.get("legacy") if isinstance(result.get("legacy"), dict) else {}
    location_node = result.get("location") if isinstance(result.get("location"), dict) else {}
    location = str(location_node.get("location") or legacy.get("location") or "").strip()
    return {
        "username": str(core.get("screen_name") or legacy.get("screen_name") or username),
        "user_id": str(result.get("rest_id") or legacy.get("id_str") or ""),
        "profile_location": location,
        "profile_location_status": "success" if location else "empty",
        "profile_location_error": "",
        "profile_location_collected_at": now_iso(),
    }


def lookup_profile_location_direct(page, username: str, user_id: str, template: dict[str, object]) -> dict[str, object]:
    url = _direct_profile_url(str(template["url"]), username, user_id)
    replay = page.evaluate(
        """async ({url, method, headers, body}) => {
            const response = await fetch(url, {
                method, headers, body: method === 'GET' ? undefined : body,
                credentials: 'include'
            });
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            return {status: response.status, ok: response.ok, payload, headers: {
                'retry-after': response.headers.get('retry-after') || '',
                'x-rate-limit-limit': response.headers.get('x-rate-limit-limit') || '',
                'x-rate-limit-reset': response.headers.get('x-rate-limit-reset') || '',
                'x-rate-limit-remaining': response.headers.get('x-rate-limit-remaining') || ''
            }};
        }""",
        {"url": url, "method": template["method"], "headers": template["headers"], "body": template.get("body")},
    )
    status = int(replay.get("status", 0))
    headers = replay.get("headers", {}) if isinstance(replay.get("headers"), dict) else {}
    if status == 429:
        raise RateLimitError(429, _retry_after_seconds(headers), "UserByScreenName")
    if status in {401, 403}:
        raise RuntimeError(f"UserByScreenName HTTP {status}")
    if not replay.get("ok"):
        raise RuntimeError(f"UserByScreenName HTTP {status or 'unknown'}")
    payload = replay.get("payload", {})
    if not isinstance(payload, dict):
        raise RuntimeError("UserByScreenName 未返回 JSON")
    author = _profile_author_from_payload(username, payload)
    author["_rate_limit"] = _rate_limit_state(headers)
    return author


def lookup_profile_location(page, username: str, user_id: str, render_wait_ms: int, direct_state: dict[str, object]) -> dict[str, object]:
    if isinstance(direct_state.get("template"), dict):
        try:
            return lookup_profile_location_direct(page, username, user_id, direct_state["template"])
        except RuntimeError as exc:
            if any(marker in str(exc) for marker in ("HTTP 401", "HTTP 403", "HTTP 429")):
                raise
            direct_state.pop("template", None)
            print(f"  用户资料直接请求模板失效，重新从页面捕获：{exc}", flush=True)
    try:
        with page.expect_response(
            lambda response: "UserByScreenName" in response.url and response.status in {200, 429},
            timeout=60_000,
        ) as pending:
            page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=90_000)
        response = pending.value
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("未捕获到 UserByScreenName JSON；登录可能失效或账号不可用") from exc
    headers = _response_headers(response)
    if response.status == 429:
        raise RateLimitError(429, _retry_after_seconds(headers), "UserByScreenName")
    payload = response.json()
    request = response.request
    forwarded = {
        "authorization", "content-type", "x-csrf-token", "x-twitter-active-user",
        "x-twitter-auth-type", "x-twitter-client-language",
    }
    direct_state["template"] = {
        "url": response.url,
        "method": request.method,
        "headers": {key: value for key, value in request.all_headers().items() if key.lower() in forwarded},
        "body": request.post_data,
    }
    print("  已捕获真实 UserByScreenName；后续作者改用资料 JSON 直接请求。", flush=True)
    author = _profile_author_from_payload(username, payload)
    author["_rate_limit"] = _rate_limit_state(headers)
    if render_wait_ms > 0:
        page.wait_for_timeout(min(render_wait_ms, 1000))
    return author


def lookup_author_direct(page, username: str, template: dict[str, object]) -> dict[str, object]:
    url = _direct_about_url(str(template["url"]), username)
    replay = page.evaluate(
        """async ({url, method, headers, body}) => {
            const response = await fetch(url, {
                method, headers, body: method === 'GET' ? undefined : body,
                credentials: 'include'
            });
            let payload = {};
            try { payload = await response.json(); } catch (_) {}
            return {status: response.status, ok: response.ok, payload, headers: {
                'retry-after': response.headers.get('retry-after') || '',
                'x-rate-limit-limit': response.headers.get('x-rate-limit-limit') || '',
                'x-rate-limit-reset': response.headers.get('x-rate-limit-reset') || '',
                'x-rate-limit-remaining': response.headers.get('x-rate-limit-remaining') || ''
            }};
        }""",
        {"url": url, "method": template["method"], "headers": template["headers"], "body": template.get("body")},
    )
    status = int(replay.get("status", 0))
    if status == 429:
        headers = replay.get("headers", {})
        raise RateLimitError(429, _retry_after_seconds(headers if isinstance(headers, dict) else {}))
    if status in {401, 403}:
        raise RuntimeError(f"AboutAccountQuery HTTP {status}")
    if not replay.get("ok"):
        raise RuntimeError(f"AboutAccountQuery HTTP {status or 'unknown'}")
    payload = replay.get("payload", {})
    if not isinstance(payload, dict):
        raise RuntimeError("AboutAccountQuery 未返回 JSON")
    author = _author_from_about(username, payload)
    headers = replay.get("headers", {})
    if isinstance(headers, dict):
        author["_rate_limit"] = _rate_limit_state(headers)
    return author


def lookup_author(page, username: str, render_wait_ms: int, direct_state: dict[str, object] | None = None) -> dict[str, object]:
    if direct_state and isinstance(direct_state.get("template"), dict):
        try:
            return lookup_author_direct(page, username, direct_state["template"])
        except RuntimeError as exc:
            if any(marker in str(exc) for marker in ("HTTP 401", "HTTP 403", "HTTP 429")):
                raise
            direct_state.pop("template", None)
            print(f"  直接请求模板失效，重新从页面捕获：{exc}", flush=True)
    page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_timeout(render_wait_ms)
    actions = page.locator('[data-testid="userActions"]')
    if not actions.count():
        raise RuntimeError("未找到账号菜单；登录可能失效或账号不可用")
    actions.click(timeout=20_000)
    item = page.get_by_text(re.compile(r"^About this account$", re.I), exact=True).last
    try:
        with page.expect_response(lambda r: "AboutAccountQuery" in r.url, timeout=60_000) as pending:
            item.click(timeout=20_000)
        response = pending.value
    except PlaywrightTimeoutError:
        with page.expect_response(lambda r: "AboutAccountQuery" in r.url, timeout=60_000) as pending:
            page.goto(f"https://x.com/{username}/about", wait_until="domcontentloaded", timeout=90_000)
        response = pending.value
    if response.status == 429:
        headers = {name: response.header_value(name) or "" for name in ("retry-after", "x-rate-limit-limit", "x-rate-limit-reset", "x-rate-limit-remaining")}
        raise RateLimitError(429, _retry_after_seconds(headers))
    if response.status in {401, 403}:
        raise RuntimeError(f"AboutAccountQuery HTTP {response.status}")
    payload = response.json()
    if direct_state is not None:
        request = response.request
        forwarded_names = {
            "authorization", "content-type", "x-csrf-token", "x-twitter-active-user",
            "x-twitter-auth-type", "x-twitter-client-language", "x-client-transaction-id",
        }
        direct_state["template"] = {
            "url": response.url,
            "method": request.method,
            "headers": {key: value for key, value in request.all_headers().items() if key.lower() in forwarded_names},
            "body": request.post_data,
        }
        print("  已捕获真实 AboutAccountQuery；后续作者改用直接请求。", flush=True)
    author = _author_from_about(username, payload)
    headers = {name: response.header_value(name) or "" for name in ("x-rate-limit-limit", "x-rate-limit-reset", "x-rate-limit-remaining")}
    author["_rate_limit"] = _rate_limit_state(headers)
    return author


def enrich_posts(posts: list[dict[str, object]], author: dict[str, object]) -> None:
    username = str(author.get("username", "")).casefold()
    for post in posts:
        if str(post.get("author_username", "")).casefold() != username:
            continue
        if author.get("user_id"):
            post["author_user_id"] = author["user_id"]
        if author.get("profile_location") and not post.get("profile_location"):
            post["profile_location"] = author["profile_location"]
            post["profile_location_collected_at"] = author.get("profile_location_collected_at", "")
        post["account_based_in"] = author.get("account_based_in", "")
        post["account_lookup_status"] = author.get("status", "failed")


def load_author_cache(output_dir: Path) -> dict[str, dict[str, object]]:
    """Reuse successfully checked authors from earlier runs in the same workspace."""
    cache: dict[str, dict[str, object]] = {}
    parent = output_dir.parent
    if not parent.exists():
        return cache
    for directory in sorted((path for path in parent.iterdir() if path.is_dir() and path != output_dir)):
        raw_path = directory / "raw.json"
        if not raw_path.exists():
            continue
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        previous = raw.get("authors", {}) if isinstance(raw, dict) else {}
        if not isinstance(previous, dict):
            continue
        for key, author in previous.items():
            if not isinstance(author, dict) or author.get("status") not in {"success", "unknown"}:
                continue
            cache[str(key).casefold()] = dict(author)
    return cache


def apply_author_cache(posts: list[dict[str, object]], authors: dict[str, dict[str, object]], cache: dict[str, dict[str, object]]) -> int:
    applied = 0
    for username in dict.fromkeys(str(post.get("author_username", "")) for post in posts if post.get("author_username")):
        key = username.casefold()
        if authors.get(key, {}).get("status") in {"success", "unknown"} or key not in cache:
            continue
        authors[key] = dict(cache[key])
        enrich_posts(posts, authors[key])
        applied += 1
    return applied


def register_pending_authors(posts: list[dict[str, object]], authors: dict[str, dict[str, object]]) -> int:
    """Persist a durable backfill queue before Account based in is requested."""
    added = 0
    for post in posts:
        username = str(post.get("author_username", "") or "").strip()
        if not username:
            continue
        key = username.casefold()
        current = authors.get(key)
        if not isinstance(current, dict):
            profile_location = str(post.get("profile_location", "") or "")
            current = {
                "username": username,
                "user_id": str(post.get("author_user_id", "") or ""),
                "profile_location": profile_location,
                "profile_location_status": "success" if profile_location else "pending",
                "profile_location_error": "",
                "profile_location_collected_at": str(post.get("profile_location_collected_at", "") or ""),
                "account_based_in": "",
                "status": "pending",
                "error": "",
                "collected_at": "",
            }
            authors[key] = current
            added += 1
        else:
            if not current.get("user_id") and post.get("author_user_id"):
                current["user_id"] = post["author_user_id"]
            if not current.get("profile_location") and post.get("profile_location"):
                current["profile_location"] = post["profile_location"]
                current["profile_location_status"] = "success"
                current["profile_location_error"] = ""
                current["profile_location_collected_at"] = post.get("profile_location_collected_at", "")
            elif current.get("profile_location") and not current.get("profile_location_status"):
                current["profile_location_status"] = "success"
    return added


def enrich_profile_location_posts(posts: list[dict[str, object]], author: dict[str, object]) -> None:
    """Apply only the public self-declared profile location; never touch Account based in."""
    username = str(author.get("username", "")).casefold()
    for post in posts:
        if str(post.get("author_username", "")).casefold() != username:
            continue
        if author.get("user_id"):
            post["author_user_id"] = author["user_id"]
        post["profile_location"] = author.get("profile_location", "")
        post["profile_location_status"] = author.get("profile_location_status", "empty")
        post["profile_location_error"] = author.get("profile_location_error", "")
        post["profile_location_collected_at"] = author.get("profile_location_collected_at", "")


def load_profile_location_cache(output_dir: Path) -> dict[str, dict[str, object]]:
    """Reuse completed UserByScreenName lookups across runs, including confirmed empty values."""
    cache: dict[str, dict[str, object]] = {}
    parent = output_dir.parent
    if not parent.exists():
        return cache
    for directory in sorted((path for path in parent.iterdir() if path.is_dir() and path != output_dir)):
        raw_path = directory / "raw.json"
        if not raw_path.exists():
            continue
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        previous = raw.get("authors", {}) if isinstance(raw, dict) else {}
        if not isinstance(previous, dict):
            continue
        for key, author in previous.items():
            if not isinstance(author, dict):
                continue
            status = str(author.get("profile_location_status", "") or "")
            if author.get("profile_location"):
                status = "success"
            if status not in {"success", "empty"}:
                continue
            cache[str(key).casefold()] = {
                "username": author.get("username", key),
                "user_id": author.get("user_id", ""),
                "profile_location": author.get("profile_location", ""),
                "profile_location_status": status,
                "profile_location_error": "",
                "profile_location_collected_at": author.get("profile_location_collected_at", ""),
            }
    return cache


def apply_profile_location_cache(posts: list[dict[str, object]], authors: dict[str, dict[str, object]], cache: dict[str, dict[str, object]]) -> int:
    applied = 0
    for key, current in authors.items():
        cache_key = str(key).casefold()
        if not isinstance(current, dict) or not profile_location_needs_lookup(current) or cache_key not in cache:
            continue
        cached = cache[cache_key]
        for field in (
            "user_id", "profile_location", "profile_location_status",
            "profile_location_error", "profile_location_collected_at",
        ):
            if cached.get(field) or field in {"profile_location", "profile_location_error"}:
                current[field] = cached.get(field, "")
        enrich_profile_location_posts(posts, current)
        applied += 1
    return applied


def enrich_author_batch(page, args: argparse.Namespace, output_dir: Path, payload: dict[str, object], posts: list[dict[str, object]], authors: dict[str, dict[str, object]], usernames: list[str], label: str, direct_state: dict[str, object]) -> int:
    pending = [
        name for name in dict.fromkeys(name for name in usernames if name)
        if authors.get(name.casefold(), {}).get("status") not in {"success", "unknown"}
    ]
    payload["active_phase"] = "enriching_authors"
    save_outputs(output_dir, payload)
    print(f"\n[{label}] 需要查询所在地的去重作者：{len(pending)} 个。", flush=True)
    for index, username in enumerate(pending, 1):
        if index > 1 and args.author_break_every > 0 and (index - 1) % args.author_break_every == 0:
            refreshed_cache = load_author_cache(output_dir)
            reused = apply_author_cache(posts, authors, refreshed_cache)
            if reused:
                print(f"[跨任务缓存] 新复用 {reused} 位作者所在地，避免重复请求。", flush=True)
            save_outputs(output_dir, payload)
            remaining = direct_state.get("rate_limit_remaining")
            reset = direct_state.get("rate_limit_reset")
            guard = max(5, args.author_break_every + 2)
            if isinstance(remaining, (int, float)) and remaining > guard:
                print(f"[响应头节奏] 当前额度剩余 {int(remaining)}，跳过固定长休息。", flush=True)
            elif isinstance(remaining, (int, float)) and isinstance(reset, (int, float)) and reset > time.time():
                pause = max(5.0, reset - time.time()) + random.uniform(5, 20)
                print(f"[响应头节奏] 当前额度剩余 {int(remaining)}，按 reset 暂停 {pause / 60:.1f} 分钟。", flush=True)
                time.sleep(pause)
            else:
                pause = random.uniform(args.author_break_min, args.author_break_max)
                print(f"[作者定期休息] 未获得可靠额度响应头；已处理 {index - 1} 位，暂停 {pause / 60:.1f} 分钟。", flush=True)
                time.sleep(pause)
        if authors.get(username.casefold(), {}).get("status") in {"success", "unknown"}:
            continue
        if index > 1:
            if direct_state.get("template"):
                time.sleep(random.uniform(args.direct_author_delay_min, args.direct_author_delay_max))
            else:
                time.sleep(random.uniform(args.author_delay_min, args.author_delay_max))
        print(f"[作者 {index}/{len(pending)}] @{username}", flush=True)
        author = None
        for attempt in range(args.author_429_retries + 1):
            wait_for_shared_about_cooldown(payload, output_dir)
            try:
                author = lookup_author(page, username, args.render_wait_ms, direct_state)
                rate_limit = author.pop("_rate_limit", {})
                if isinstance(rate_limit, dict):
                    if "remaining" in rate_limit:
                        direct_state["rate_limit_remaining"] = rate_limit["remaining"]
                    if "reset" in rate_limit:
                        direct_state["rate_limit_reset"] = rate_limit["reset"]
                print(f"  所在地：{author['account_based_in'] or 'X 未公开'}", flush=True)
                break
            except RateLimitError as exc:
                if attempt >= args.author_429_retries:
                    message = f"{type(exc).__name__}: {exc}；自动冷却重试已耗尽"
                    author = {"username": username, "user_id": "", "account_based_in": "", "status": "failed", "error": message, "collected_at": now_iso()}
                    print(f"  查询失败：{message}", flush=True)
                    break
                fallback = min(args.author_429_max_cooldown, args.author_429_base_cooldown * (2 ** attempt))
                cooldown = max(exc.retry_after or 0, fallback) + random.uniform(15, 90)
                until = set_shared_about_cooldown(cooldown, f"AboutAccountQuery HTTP 429；@{username}")
                direct_state.pop("template", None)
                payload["active_phase"] = "rate_limited"
                payload["cooldown_until"] = datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")
                save_outputs(output_dir, payload)
                print(f"  触发 429：不切换账号、不立即重试；全节点冷却 {cooldown / 60:.1f} 分钟后自动继续（第 {attempt + 1}/{args.author_429_retries} 次）。", flush=True)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                author = {"username": username, "user_id": "", "account_based_in": "", "status": "failed", "error": message, "collected_at": now_iso()}
                print(f"  查询失败：{message}", flush=True)
                break
        if author is None:
            raise RuntimeError("作者查询未产生结果")
        existing_author = authors.get(username.casefold(), {})
        if isinstance(existing_author, dict):
            if not author.get("user_id") and existing_author.get("user_id"):
                author["user_id"] = existing_author["user_id"]
            for field in ("profile_location", "profile_location_collected_at"):
                if existing_author.get(field):
                    author[field] = existing_author[field]
        authors[username.casefold()] = author
        enrich_posts(posts, author)
        save_checkpoint(output_dir, payload)
        if any(marker in str(author.get("error", "")) for marker in ("HTTP 401", "HTTP 403", "HTTP 429")):
            raise RuntimeError(str(author["error"]))
    payload["active_phase"] = "searching"
    save_outputs(output_dir, payload)
    return len(pending)


def enrich_profile_location_batch(
    page,
    args: argparse.Namespace,
    output_dir: Path,
    payload: dict[str, object],
    posts: list[dict[str, object]],
    authors: dict[str, dict[str, object]],
) -> int:
    """Backfill self-declared profile locations through observed UserByScreenName JSON."""
    pending = [
        str(author.get("username", key))
        for key, author in authors.items()
        if isinstance(author, dict) and profile_location_needs_lookup(author)
    ]
    payload["active_phase"] = "profile_location_enriching"
    save_outputs(output_dir, payload)
    print(f"\n[快速补自填位置] 待查询去重作者：{len(pending)} 个。", flush=True)
    direct_state: dict[str, object] = {}
    processed = 0
    for index, username in enumerate(pending, 1):
        key = username.casefold()
        current = authors.get(key, {})
        if not isinstance(current, dict) or not profile_location_needs_lookup(current):
            continue
        if index > 1:
            remaining = direct_state.get("rate_limit_remaining")
            reset = direct_state.get("rate_limit_reset")
            if isinstance(remaining, (int, float)) and remaining <= 2 and isinstance(reset, (int, float)) and reset > time.time():
                pause = max(5.0, reset - time.time()) + random.uniform(5, 20)
                print(f"[资料响应头节奏] 额度剩余 {int(remaining)}，等待 {pause / 60:.1f} 分钟。", flush=True)
                time.sleep(pause)
            else:
                time.sleep(random.uniform(args.profile_delay_min, args.profile_delay_max))
        print(f"[自填位置 {index}/{len(pending)}] @{username}", flush=True)
        result: dict[str, object] | None = None
        fatal_error = False
        for attempt in range(args.author_429_retries + 1):
            try:
                result = lookup_profile_location(
                    page,
                    username,
                    str(current.get("user_id", "") or ""),
                    args.render_wait_ms,
                    direct_state,
                )
                rate_limit = result.pop("_rate_limit", {})
                if isinstance(rate_limit, dict):
                    if "remaining" in rate_limit:
                        direct_state["rate_limit_remaining"] = rate_limit["remaining"]
                    if "reset" in rate_limit:
                        direct_state["rate_limit_reset"] = rate_limit["reset"]
                print(f"  自填位置：{result.get('profile_location') or '未填写'}", flush=True)
                break
            except RateLimitError as exc:
                if attempt >= args.author_429_retries:
                    message = f"{type(exc).__name__}: {exc}；自动冷却重试已耗尽"
                    result = {
                        "username": username, "user_id": current.get("user_id", ""),
                        "profile_location": "", "profile_location_status": "failed",
                        "profile_location_error": message, "profile_location_collected_at": now_iso(),
                    }
                    fatal_error = True
                    print(f"  查询失败：{message}", flush=True)
                    break
                fallback = min(args.author_429_max_cooldown, args.author_429_base_cooldown * (2 ** attempt))
                cooldown = max(exc.retry_after or 0, fallback) + random.uniform(15, 90)
                direct_state.pop("template", None)
                payload["active_phase"] = "profile_location_rate_limited"
                payload["cooldown_until"] = datetime.fromtimestamp(time.time() + cooldown).astimezone().isoformat(timespec="seconds")
                save_outputs(output_dir, payload)
                print(f"  UserByScreenName 触发 429；按响应头/退避等待 {cooldown / 60:.1f} 分钟。", flush=True)
                time.sleep(cooldown)
                payload.pop("cooldown_until", None)
                payload["active_phase"] = "profile_location_enriching"
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                result = {
                    "username": username, "user_id": current.get("user_id", ""),
                    "profile_location": "", "profile_location_status": "failed",
                    "profile_location_error": message, "profile_location_collected_at": now_iso(),
                }
                fatal_error = any(marker in message for marker in ("HTTP 401", "HTTP 403", "登录可能失效"))
                print(f"  查询失败：{message}", flush=True)
                break
        if result is None:
            raise RuntimeError("自填位置查询未产生结果")
        if not result.get("user_id") and current.get("user_id"):
            result["user_id"] = current["user_id"]
        for field, value in result.items():
            if not str(field).startswith("_"):
                current[field] = value
        authors[key] = current
        enrich_profile_location_posts(posts, current)
        processed += 1
        if processed % args.profile_checkpoint_every == 0 or fatal_error:
            save_checkpoint(output_dir, payload)
        if fatal_error:
            raise RuntimeError(str(result.get("profile_location_error", "自填位置查询失败")))
    payload.pop("cooldown_until", None)
    payload["profile_location_backlog_count"] = len(profile_location_queue(authors))
    payload["active_phase"] = "completed_with_author_backlog" if any(
        isinstance(author, dict) and author.get("status") in {"pending", "failed"}
        for author in authors.values()
    ) else "completed"
    save_outputs(output_dir, payload)
    return processed


def collect_profile_locations(args: argparse.Namespace, output_dir: Path) -> None:
    raw_path = output_dir / "raw.json"
    if not raw_path.exists():
        raise RuntimeError("找不到 raw.json，无法补全自填位置")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("raw.json 格式无效")
    posts = [post for post in payload.get("posts", []) if isinstance(post, dict)]
    authors = payload.get("authors", {})
    authors = dict(authors) if isinstance(authors, dict) else {}
    payload["posts"], payload["authors"] = posts, authors
    register_pending_authors(posts, authors)
    cached = apply_profile_location_cache(posts, authors, load_profile_location_cache(output_dir))
    if cached:
        print(f"[跨任务缓存] 已复用 {cached} 位作者的自填位置结果。", flush=True)
    queue = profile_location_queue(authors)
    if not queue:
        print("没有待补的自填位置作者。", flush=True)
        save_outputs(output_dir, payload)
        return
    print("模式：仅快速补全 profile_location；不会搜索帖子，也不会请求 Account based in。", flush=True)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(args.session_dir.resolve()), headless=False, executable_path=str(CHROME),
            locale="en-US", viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(context, page, args.login_timeout)
        enrich_profile_location_batch(page, args, output_dir, payload, posts, authors)
        context.close()


def collect(args: argparse.Namespace, keywords: list[str], output_dir: Path) -> None:
    timings = current_timings() or StageTimings()
    with timing_scope(timings):
        try:
            with measure("run_total"):
                return _collect_browser_run(args, keywords, output_dir)
        finally:
            _write_json_atomic(output_dir / "timings.json", timings.snapshot())


def _collect_browser_run(args: argparse.Namespace, keywords: list[str], output_dir: Path) -> None:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    raw_path = output_dir / "raw.json"
    if args.resume and raw_path.exists():
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        if payload.get("guard_hold"):
            raise RuntimeError("任务因数据质量异常被保护暂停；请先核验日期查询与响应，再人工解除 guard_hold，禁止自动恢复。")
        if payload.get("requested_keywords") != keywords:
            raise RuntimeError("断点关键词列表与当前输入不一致")
    else:
        payload = {"requested_keywords": keywords, "search_runs": [], "posts": [], "authors": {}, "finalized": False, "generated_at": now_iso()}
    payload["search_conditions"] = {
        "start_date": args.start_date or "", "end_date": args.end_date or "", "search_type": args.search_type,
        "slice_days": args.slice_days, "no_growth_stop": args.no_growth_stop, "timezone_offset": args.timezone_offset,
        "author_strategy": args.author_strategy, "search_engine": args.search_engine,
    }
    posts: list[dict[str, object]] = list(payload.get("posts", []))
    authors: dict[str, dict[str, object]] = dict(payload.get("authors", {}))
    payload["posts"], payload["authors"] = posts, authors
    register_pending_authors(posts, authors)
    profile_cache = load_profile_location_cache(output_dir)
    profile_cached_count = apply_profile_location_cache(posts, authors, profile_cache)
    if profile_cached_count:
        print(f"已复用历史批次作者自填位置：{profile_cached_count} 位。", flush=True)
    author_cache = load_author_cache(output_dir)
    cached_count = apply_author_cache(posts, authors, author_cache)
    if cached_count:
        print(f"已复用历史批次作者所在地：{cached_count} 位。", flush=True)
    save_outputs(output_dir, payload)

    slices = make_date_slices(start_date, end_date, args.slice_days)
    work_items = [(keyword_index, keyword, slice_start, slice_end) for keyword_index, keyword in enumerate(keywords, 1) for slice_start, slice_end in slices]
    config_path = output_dir / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    payload["repair_policy"] = config.get("repair_policy", payload.get("repair_policy", "strict"))
    if args.resume and payload["repair_policy"] == "coverage_first":
        settled = settle_saved_in_progress(payload.get("search_runs", []))
        if settled:
            print(f"[旧断点结算] {settled} 个已有样本日期保留为中断部分样本；原因未知，不重跑。", flush=True)
    completed = (accepted_search_keys(payload.get("search_runs", []))
                 if payload["repair_policy"] == "coverage_first"
                 else reliable_search_keys(payload.get("search_runs", [])))
    save_checkpoint(output_dir, payload)
    if payload["repair_policy"] == "coverage_first":
        payload["data_quality_warning"] = "覆盖优先样本：部分日期分页中断后保留已有帖子，不代表当天全量；失败且零条的日期仍需补跑。"
        attempted = {search_run_key(run) for run in payload.get("search_runs", []) if isinstance(run, dict)}
        work_items.sort(key=lambda item: (item[1], item[2].isoformat() if item[2] else "", item[3].isoformat() if item[3] else "") in attempted)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(args.session_dir.resolve()), headless=False, executable_path=str(CHROME),
            locale="en-US", viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(context, page, args.login_timeout)
        direct_state: dict[str, object] = {}

        if args.author_strategy == "per_slice" and posts:
            backlog = list(dict.fromkeys(str(post.get("author_username", "")) for post in posts if post.get("author_username")))
            enrich_author_batch(page, args, output_dir, payload, posts, authors, backlog, "补齐历史日期作者", direct_state)

        completed_this_run = 0
        for task_index, (keyword_index, keyword, slice_start, slice_end) in enumerate(work_items, 1):
            task_key = (keyword, slice_start.isoformat() if slice_start else "", slice_end.isoformat() if slice_end else "")
            if task_key in completed:
                print(f"[跳过搜索] {keyword} {task_key[1]}—{task_key[2]}", flush=True)
                continue
            if payload.get("search_runs"):
                time.sleep(random.uniform(args.delay_min, args.delay_max))
            if completed_this_run and args.break_every > 0 and completed_this_run % args.break_every == 0:
                pause = random.uniform(args.break_min, args.break_max)
                print(f"[定期休息] 已完成 {completed_this_run} 个搜索片，暂停 {pause / 60:.1f} 分钟。", flush=True)
                time.sleep(pause)
            period = f" {task_key[1]}—{task_key[2]}" if task_key[1] or task_key[2] else ""
            print(f"\n[搜索片 {task_index}/{len(work_items)}] {keyword}{period}", flush=True)
            payload["active_phase"] = "searching"
            # Only phase changed: keep the durable checkpoint/current status,
            # do not rebuild identical CSV/HTML/ZIP before the next search.
            save_checkpoint(output_dir, payload)
            search_started_at = now_iso()
            search_started_monotonic = time.monotonic()
            last_error: Exception | None = None
            page_progress: dict[str, object] = {}
            progress_rows: list[dict[str, object]] = []
            persisted_keys = {(str(p.get("keyword", "")), str(p.get("post_id", ""))) for p in posts}
            provisional_run: dict[str, object] = {}
            row_delta = RowDeltaTracker()

            def persist_page(rows, progress):
                nonlocal progress_rows, page_progress
                # The pipeline owns an append-only list; retain it, do not copy
                # and rescan every historical page on each new checkpoint.
                progress_rows = rows
                page_progress = dict(progress)
                delta = row_delta.pending(rows)
                for row in delta:
                    key = (keyword, str(row.get("post_id", "")))
                    if key not in persisted_keys:
                        posts.append(row)
                        persisted_keys.add(key)
                register_pending_authors(delta, authors)
                if rows:
                    if not provisional_run:
                        payload["search_runs"].append(provisional_run)
                    provisional_run.update({"keyword": keyword, "keyword_index": keyword_index, "started_at": search_started_at, **progress, "checkpoint_at": now_iso()})
                payload["inflight_search"] = {"keyword": keyword, **progress, "checkpoint_at": now_iso()}
                save_checkpoint(output_dir, payload)
                row_delta.acknowledge(rows)

            for attempt in range(args.retry_limit + 1):
                try:
                    if args.search_engine == "scroll":
                        rows, coverage = collect_search(page, keyword, keyword_index, args.limit, args.max_scrolls, args.render_wait_ms, output_dir, slice_start, slice_end, args.search_type, args.no_growth_stop, args.scroll_delay_min, args.scroll_delay_max, args.timezone_offset)
                        coverage["search_engine"] = "scroll"
                    else:
                        try:
                            rows, coverage = collect_search_graphql(page, keyword, keyword_index, args.limit, args.max_scrolls, output_dir, slice_start, slice_end, args.search_type, args.no_growth_stop, args.graphql_delay_min, args.graphql_delay_max, args.timezone_offset, args.pagination_trigger_attempts, args.pagination_trigger_timeout_ms, persist_page)
                        except SearchRateLimitError:
                            raise
                        except Exception as exc:
                            if args.search_engine != "auto":
                                raise
                            print(f"  [高速模式回退] {type(exc).__name__}: {exc}；本片改用页面滚动。", flush=True)
                            rows, coverage = collect_search(page, keyword, keyword_index, args.limit, args.max_scrolls, args.render_wait_ms, output_dir, slice_start, slice_end, args.search_type, args.no_growth_stop, args.scroll_delay_min, args.scroll_delay_max, args.timezone_offset)
                            coverage["search_engine"] = "scroll_fallback"
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, OSError):
                        # A disk failure is not a recoverable network retry.
                        raise
                    if any(f"SearchTimeline HTTP {code}" in str(exc) for code in (401, 403, 404)):
                        payload["guard_hold"] = {"reason": "search_http_requires_review", "detected_at": now_iso(), "error": str(exc)}
                        payload["active_phase"] = "paused_quality_review"
                        payload["finalized"] = False
                        save_checkpoint(output_dir, payload)
                        context.close()
                        raise
                    if attempt >= args.retry_limit and not progress_rows:
                        raise
                    factor = attempt + 1
                    if "429" in str(exc) or "限流" in str(exc):
                        factor *= 2
                    pause = random.uniform(args.backoff_min, args.backoff_max) * factor
                    if isinstance(exc, SearchRateLimitError) and exc.retry_after:
                        pause = max(pause, exc.retry_after + random.uniform(10, 45))
                    pause = max(pause, getattr(exc, "search_retry_after", 0) + random.uniform(10, 45))
                    print(f"[退避重试 {attempt + 1}/{args.retry_limit}] {type(exc).__name__}: {exc}；暂停 {pause / 60:.1f} 分钟。", flush=True)
                    time.sleep(pause)
                    if progress_rows and payload["repair_policy"] == "coverage_first":
                        rows = progress_rows
                        coverage = {**page_progress, "stop_reason": "interrupted_with_saved_sample", "coverage_status": "needs_retry", "interruption_error": f"{type(exc).__name__}: {exc}"}
                        last_error = None
                        print(f"[部分样本保留] {len(rows)} 条已落盘；遵守退避后继续下一日期，不从头重跑有样本日期。", flush=True)
                        break
            if last_error is not None:
                raise last_error
            elapsed_seconds = max(0.001, time.monotonic() - search_started_monotonic)
            coverage["started_at"] = search_started_at
            coverage["elapsed_seconds"] = round(elapsed_seconds, 3)
            coverage["posts_per_minute"] = round(len(rows) * 60 / elapsed_seconds, 2)
            existing_keys = {(str(p.get("keyword", "")), str(p.get("post_id", ""))) for p in posts}
            for row in rows:
                key = (keyword, str(row.get("post_id", "")))
                if key not in existing_keys:
                    posts.append(row); existing_keys.add(key)
            queued = register_pending_authors(rows, authors)
            if queued:
                print(f"  已保存 {queued} 位新作者的稳定 ID 与所在地补全队列。", flush=True)
            profile_cached_count = apply_profile_location_cache(posts, authors, profile_cache)
            if profile_cached_count:
                print(f"  复用历史批次作者自填位置：{profile_cached_count} 位。", flush=True)
            cached_count = apply_author_cache(posts, authors, author_cache)
            if cached_count:
                print(f"  复用历史批次作者所在地：{cached_count} 位。", flush=True)
            final_record = {"keyword": keyword, "keyword_index": keyword_index, **coverage, "session_profile":str(args.session_dir.resolve()), "completed_at": now_iso()}
            if provisional_run:
                provisional_run.update(final_record)
            else:
                payload["search_runs"].append(final_record)
            payload.pop("inflight_search", None)
            save_outputs(output_dir, payload)
            print(f"  搜索片完成：{len(rows)} 条，停止原因 {coverage['stop_reason']}，断点已保存。", flush=True)
            if coverage['stop_reason'] == 'date_filter_mismatch':
                payload['guard_hold'] = {'reason': 'date_filter_mismatch', 'detected_at': now_iso(), 'slice_start': task_key[1], 'slice_end': task_key[2], 'returned_date_examples': coverage.get('returned_date_examples', [])}
                payload['finalized'] = False
                payload['active_phase'] = 'paused_quality_review'
                save_outputs(output_dir, payload)
                context.close()
                raise RuntimeError('[数据质量保护暂停] 响应含帖子但全部被日期过滤；已保存诊断与断点，不推进下一日期，不自动重试。')
            if args.author_strategy == "per_slice":
                slice_authors = list(dict.fromkeys(str(row.get("author_username", "")) for row in rows if row.get("author_username")))
                enrich_author_batch(page, args, output_dir, payload, posts, authors, slice_authors, f"日期片 {task_key[1]}—{task_key[2]} 作者", direct_state)
            completed_this_run += 1

        repair_queue = search_repair_queue(payload.get("search_runs", []), str(payload.get("repair_policy", "strict")))
        if repair_queue:
            payload["repair_queue_count"] = len(repair_queue)
            payload["finalized"] = False
            payload["active_phase"] = "repair_pending"
            print(
                f"[异常日期队列] {len(repair_queue)} 个搜索片未收到完整网络响应；"
                "已保留部分帖子和断点，可在工作台一键补跑。",
                flush=True,
            )
            save_outputs(output_dir, payload)
            context.close()
            return

        unique_authors = list(dict.fromkeys(str(p.get("author_username", "")) for p in posts if p.get("author_username")))
        if args.author_strategy != "deferred":
            enrich_author_batch(page, args, output_dir, payload, posts, authors, unique_authors, "任务收尾作者", direct_state)
        else:
            pending_count = sum(1 for author in authors.values() if isinstance(author, dict) and author.get("status") == "pending")
            payload["author_backlog_count"] = pending_count
            print(f"[延后补全] 帖子采集已完成，{pending_count} 位作者保存在待补队列；可稍后一键补全 Account based in。", flush=True)

        payload["finalized"] = True
        payload["repair_queue_count"] = 0
        payload["profile_location_backlog_count"] = len(profile_location_queue(authors))
        payload["active_phase"] = "completed_with_author_backlog" if args.author_strategy == "deferred" and payload.get("author_backlog_count") else "completed"
        payload["completed_at"] = now_iso()
        save_outputs(output_dir, payload)
        context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="关键词检索 X 帖子，并补充作者 Account based in。")
    parser.add_argument("--keywords-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-scrolls", type=int, default=12)
    parser.add_argument("--render-wait-ms", type=int, default=3000)
    parser.add_argument("--delay-min", type=float, default=5)
    parser.add_argument("--delay-max", type=float, default=10)
    parser.add_argument("--scroll-delay-min", type=float, default=3)
    parser.add_argument("--scroll-delay-max", type=float, default=6)
    parser.add_argument("--search-engine", choices=["scroll", "graphql", "auto"], default="scroll", help="页面滚动、GraphQL JSON，或失败时自动回退")
    parser.add_argument("--graphql-delay-min", type=float, default=2)
    parser.add_argument("--graphql-delay-max", type=float, default=4)
    parser.add_argument("--pagination-trigger-attempts", type=int, default=3, help="高速 JSON 翻页未触发时的 UI 重试次数")
    parser.add_argument("--pagination-trigger-timeout-ms", type=int, default=15000, help="每次等待当前查询 SearchTimeline 响应的毫秒数")
    parser.add_argument("--break-every", type=int, default=20)
    parser.add_argument("--break-min", type=float, default=120)
    parser.add_argument("--break-max", type=float, default=240)
    parser.add_argument("--retry-limit", type=int, default=3)
    parser.add_argument("--backoff-min", type=float, default=120)
    parser.add_argument("--backoff-max", type=float, default=300)
    parser.add_argument("--author-delay-min", type=float, default=8)
    parser.add_argument("--author-delay-max", type=float, default=15)
    parser.add_argument("--direct-author-delay-min", type=float, default=4)
    parser.add_argument("--direct-author-delay-max", type=float, default=8)
    parser.add_argument("--author-break-every", type=int, default=15)
    parser.add_argument("--author-break-min", type=float, default=180)
    parser.add_argument("--author-break-max", type=float, default=360)
    parser.add_argument("--author-429-retries", type=int, default=3)
    parser.add_argument("--author-429-base-cooldown", type=float, default=1800)
    parser.add_argument("--author-429-max-cooldown", type=float, default=7200)
    parser.add_argument("--author-strategy", choices=["per_slice", "after_search", "deferred"], default="per_slice", help="逐片查询、搜索后查询，或保存队列稍后补全")
    parser.add_argument("--profile-location-backfill-only", action="store_true", help="仅补全用户主页自填位置，不搜索帖子、不查询 Account based in")
    parser.add_argument("--profile-delay-min", type=float, default=2)
    parser.add_argument("--profile-delay-max", type=float, default=4)
    parser.add_argument("--profile-checkpoint-every", type=int, default=20)
    parser.add_argument("--login-timeout", type=int, default=900)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--search-type", choices=["live", "top"], default="live")
    parser.add_argument("--slice-days", type=int, default=0, help="按多少天拆分日期范围；0 表示不拆分")
    parser.add_argument("--no-growth-stop", type=int, default=5, help="连续多少次滚动无新增后停止")
    parser.add_argument("--timezone-offset", type=float, default=8, help="按哪个 UTC 偏移解释自然日；中国标准时间为 8")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 0 <= args.limit <= 100_000 or not 1 <= args.max_scrolls <= 10_000:
            raise ValueError("每片目标必须为 0—100000（0 表示不限），滚动次数必须为 1—10000")
        if not 1 <= args.pagination_trigger_attempts <= 8:
            raise ValueError("分页触发重试次数必须为 1—8")
        if not 5_000 <= args.pagination_trigger_timeout_ms <= 60_000:
            raise ValueError("分页响应等待必须为 5000—60000 毫秒")
        if not 0 <= args.slice_days <= 3660 or not 2 <= args.no_growth_stop <= 50:
            raise ValueError("日期切片或无新增停止次数无效")
        if not -12 <= args.timezone_offset <= 14:
            raise ValueError("时区偏移必须在 UTC-12 到 UTC+14 之间")
        if (
            not 0 <= args.delay_min <= args.delay_max
            or not 2 <= args.author_delay_min <= args.author_delay_max
            or not 1 <= args.direct_author_delay_min <= args.direct_author_delay_max <= 300
            or not 0.5 <= args.profile_delay_min <= args.profile_delay_max <= 300
        ):
            raise ValueError("等待区间无效")
        if not 0 <= args.author_break_every <= 10000 or not 0 <= args.author_break_min <= args.author_break_max <= 86400:
            raise ValueError("作者定期休息参数无效")
        if not 0 <= args.author_429_retries <= 10 or not 60 <= args.author_429_base_cooldown <= args.author_429_max_cooldown <= 86400:
            raise ValueError("作者 429 冷却参数无效")
        if not 1 <= args.profile_checkpoint_every <= 1000:
            raise ValueError("自填位置断点频率必须为 1—1000")
        if not 1 <= args.scroll_delay_min <= args.scroll_delay_max <= 300:
            raise ValueError("滚动等待区间无效")
        if not 0.2 <= args.graphql_delay_min <= args.graphql_delay_max <= 300:
            raise ValueError("GraphQL 请求等待区间无效")
        if not 0 <= args.break_every <= 10000 or not 0 <= args.break_min <= args.break_max <= 86400:
            raise ValueError("定期休息参数无效")
        if not 0 <= args.retry_limit <= 10 or not 10 <= args.backoff_min <= args.backoff_max <= 86400:
            raise ValueError("退避重试参数无效")
        start_date, end_date = parse_date(args.start_date), parse_date(args.end_date)
        if start_date and end_date and start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期")
        keywords = read_keywords(args.keywords_file)
        if not keywords:
            raise ValueError("没有读取到关键词")
        print(f"关键词数：{len(keywords)}", flush=True)
        if args.profile_location_backfill_only:
            print("任务类型：仅快速补全主页自填位置（不搜索帖子、不查询 Account based in）", flush=True)
        else:
            print(f"每个搜索片目标：{args.limit if args.limit > 0 else '不限量'}", flush=True)
            print(f"日期切片：{str(args.slice_days) + ' 天/片' if args.slice_days > 0 else '不切片'}", flush=True)
            print(f"搜索引擎：{args.search_engine}", flush=True)
            print(f"Account based in：{'延后补全' if args.author_strategy == 'deferred' else '当前任务补全'}", flush=True)
        print(f"输出目录：{args.output.resolve()}", flush=True)
        if args.dry_run:
            print("DRY RUN：输入校验通过，未访问 X。", flush=True)
            return 0
        if args.profile_location_backfill_only:
            collect_profile_locations(args, args.output.resolve())
            print("自填位置快速补全处理完成；Account based in 队列保持不变。", flush=True)
        else:
            collect(args, keywords, args.output.resolve())
            print("全部关键词、帖子和作者所在地处理完成。", flush=True)
        return 0
    except KeyboardInterrupt:
        print("用户中止；已完成内容已保存，可断点续跑。", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
