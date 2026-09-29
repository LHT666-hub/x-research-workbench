from __future__ import annotations

import argparse
import csv
import html
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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_DIR = ROOT / "tmp" / "chrome-x-keyword-profile"
OUTPUT_ROOT = ROOT / "outputs" / "x_keyword_runs"
SHARED_ABOUT_COOLDOWN = ROOT / "tmp" / "x-about-cooldown.json"


class RateLimitError(RuntimeError):
    def __init__(self, status: int = 429, retry_after: float | None = None) -> None:
        super().__init__(f"AboutAccountQuery HTTP {status}")
        self.status = status
        self.retry_after = retry_after


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
    "author_user_id", "account_based_in", "source", "account_lookup_status",
    "collected_at",
]

DEDUP_FIELDS = [
    "post_id", "post_url", "post_text", "posted_at", "time_label",
    "author_username", "author_display_name", "author_user_id",
    "account_based_in", "matched_keywords", "keyword_count", "source",
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


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def build_search_query(keyword: str, start_date: date | None, end_date: date | None) -> str:
    parts = [keyword.strip()]
    if start_date:
        parts.append(f"since:{start_date.isoformat()}")
    if end_date:
        # Current X web search treats the selected "to" date as inclusive.
        parts.append(f"until:{end_date.isoformat()}")
    return " ".join(parts)


def post_in_date_range(posted_at: str, start_date: date | None, end_date: date | None, timezone_offset: float = 8) -> bool:
    if not posted_at:
        return True
    try:
        local_tz = timezone(timedelta(hours=timezone_offset))
        value = datetime.fromisoformat(posted_at.replace("Z", "+00:00")).astimezone(local_tz).date()
    except ValueError:
        return True
    return (not start_date or value >= start_date) and (not end_date or value <= end_date)


def make_date_slices(start_date: date | None, end_date: date | None, slice_days: int) -> list[tuple[date | None, date | None]]:
    if not start_date or not end_date or slice_days <= 0:
        return [(start_date, end_date)]
    output: list[tuple[date, date]] = []
    cursor = start_date
    while cursor <= end_date:
        slice_end = min(end_date, cursor + timedelta(days=slice_days - 1))
        output.append((cursor, slice_end))
        cursor = slice_end + timedelta(days=1)
    return output


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
        for field in ("author_user_id", "account_based_in", "author_display_name"):
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
    exhausted_count = sum(run.get("coverage_status") == "apparently_exhausted" for run in coverage)
    truncated_count = sum(run.get("coverage_status") == "truncated" for run in coverage)
    coverage_rows = "".join(
        "<tr><td>{}</td><td>{} — {}</td><td>{}</td><td>{}</td><td>{}</td><td><span class='tag {}'>{}</span></td></tr>".format(
            html.escape(str(run.get("keyword", ""))), html.escape(str(run.get("slice_start", "开放")) or "开放"),
            html.escape(str(run.get("slice_end", "开放")) or "开放"), run.get("count", 0), run.get("scrolls_used", ""),
            html.escape(str(run.get("stop_reason", "旧版记录"))), "ok" if run.get("coverage_status") == "apparently_exhausted" else "warn",
            "滚动无新增" if run.get("coverage_status") == "apparently_exhausted" else "被预算截断",
        ) for run in coverage
    ) or "<tr><td colspan='6'>暂无覆盖记录</td></tr>"
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
    resolved_authors = len([row for row in payload.get("authors", {}).values() if isinstance(row, dict)]) if isinstance(payload.get("authors"), dict) else 0
    unique_author_count = len({str(row.get("author_username", "")).casefold() for row in deduped if row.get("author_username")})
    if payload.get("finalized"):
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
<body><div class='shell'><aside data-od-id='sidebar'><div class='brand'>X Research Lab<small>关键词帖子研究工作台</small></div><nav class='nav'><a href='/'>← 返回采集工作台</a><a class='active' href='#overview'>研究概览</a><a href='#dataset'>筛选帖子</a><a href='#coverage'>覆盖诊断</a><a href='#distribution'>分组统计</a><a href='posts.csv'>原始命中</a><a href='research_coding_template.csv'>编码模板</a><a href='all_exports.zip'>完整导出</a></nav><div class='side-note'>Account based in 来源：<br>About this account<br><br>页面生成时间：__UPDATED__</div></aside>
<div class='content'><header data-od-id='topbar'><div><h1>X / Twitter 关键词研究结果</h1><span>批次 __RUN_ID__ · 原始记录保留 · 研究样本自动去重</span></div><div class='top-actions'><select id='runSwitcher' aria-label='切换历史批次'><option value=''>正在载入历史批次…</option></select><a class='button' href='#dataset'>筛选帖子</a></div></header><main>
<section id='overview' class='kpis' data-od-id='kpi-cards'><div class='card'><span>去重帖子</span><b>__DEDUP_COUNT__</b><em>按 post_id 自动合并</em></div><div class='card'><span>原始关键词命中</span><b>__RAW_COUNT__</b><em>保留交叉命中关系</em></div><div class='card'><span>去重作者</span><b>__AUTHOR_COUNT__</b><em>作者所在地只查一次</em></div><div class='card'><span>所在地有效率</span><b>__KNOWN_RATE__%</b><em>__KNOWN_COUNT__ / __DEDUP_COUNT__ 条</em></div></section>
<div class='notice' id='locationNotice'><strong>所在地状态：</strong>__LOCATION_NOTICE__</div>
<section class='panel' style='margin-top:14px' data-od-id='exports'><h2>一键导出</h2><div class='exports'><a class='button' href='all_exports.zip'>下载全部 ZIP</a><a class='button secondary' href='posts_deduplicated.csv'>去重样本 CSV</a><a class='button secondary' href='posts.csv'>原始命中 CSV</a><a class='button secondary' href='research_dataset.json'>研究样本 JSON</a><a class='button secondary' href='research_coding_template.csv'>内容编码模板</a><button class='button secondary' id='filteredExport'>导出当前筛选</button></div></section>
<section id='dataset' class='panel' style='margin-top:14px' data-od-id='dataset'><h2>帖子筛选与浏览 <span class='count' id='visibleCount'></span></h2><div class='filter-shell'><div class='filter-grid'><label class='filter-field wide'><span>正文或关键词</span><input id='search' placeholder='输入 GPT、模型名称或正文片段'></label><label class='filter-field'><span>作者用户名</span><input id='authorSearch' placeholder='例如 openai'></label><label class='filter-field'><span>所在地状态</span><select id='locationStatus'><option value=''>全部状态</option><option value='known'>已有国家/地区</option><option value='__pending__'>待查询</option><option value='__unavailable__'>X 未公开</option><option value='__failed__'>查询失败</option></select></label><label class='filter-field'><span>国家 / 地区</span><select id='country'><option value=''>全部国家/地区</option>__COUNTRY_OPTIONS__</select></label><label class='filter-field'><span>命中关键词</span><select id='keyword'><option value=''>全部关键词</option>__KEYWORD_OPTIONS__</select></label><label class='filter-field'><span>发帖日期从</span><input id='dateFrom' type='date'></label><label class='filter-field'><span>发帖日期至</span><input id='dateTo' type='date'></label><label class='filter-field'><span>排序</span><select id='sortOrder'><option value='newest'>时间：最新优先</option><option value='oldest'>时间：最早优先</option><option value='author'>作者：A–Z</option><option value='country'>国家：A–Z</option></select></label><label class='filter-field'><span>每页显示</span><select id='pageSize'><option value='25'>25 条</option><option value='50'>50 条</option><option value='100' selected>100 条</option><option value='250'>250 条</option></select></label></div><div class='filter-actions'><span class='hint' id='activeFilterSummary'>当前显示全部去重帖子</span><button class='button secondary' id='reset'>清除全部筛选</button></div></div><div class='filter-summary'><div class='filter-stat'><b id='filteredCount'>0</b>筛选后帖子</div><div class='filter-stat'><b id='filteredAuthors'>0</b>筛选后作者</div><div class='filter-stat'><b id='filteredCountries'>0</b>筛选后国家 / 地区</div></div><div class='table'><table><thead><tr><th>命中关键词</th><th>帖子原文</th><th>发帖时间</th><th>作者</th><th>账号所在地</th><th>帖子 ID</th><th>链接</th></tr></thead><tbody id='rows'></tbody></table></div><div class='pager'><button class='button secondary' id='prevPage'>上一页</button><span class='pager-center' id='pageInfo'>第 1 页</span><button class='button secondary' id='nextPage'>下一页</button></div></section>
<section id='coverage' class='panel' style='margin-top:14px'><h2>搜索覆盖诊断</h2><p class='empty'>X 不提供总命中数。“滚动无新增”表示本次网页会话看起来已到底，但不等于平台数据库绝对全量；“被预算截断”表示目标条数或滚动上限先到了。</p><div class='exports'><span class='button secondary'>搜索片 __SLICE_COUNT__</span><span class='button secondary'>滚动无新增 __EXHAUSTED_COUNT__</span><span class='button secondary'>被预算截断 __TRUNCATED_COUNT__</span><a class='button secondary' href='coverage_report.csv'>下载覆盖报告</a></div><div class='table' style='max-height:320px;margin-top:12px'><table><thead><tr><th>关键词</th><th>日期片</th><th>帖子数</th><th>滚动次数</th><th>停止原因</th><th>诊断</th></tr></thead><tbody>__COVERAGE_ROWS__</tbody></table></div></section>
<section id='distribution' class='charts' data-od-id='charts'><article class='panel'><h2>账号所在地分布</h2>__COUNTRY_CHART__</article><article class='panel'><h2>关键词命中分布</h2>__KEYWORD_CHART__</article></section><section class='panel' style='margin-top:14px'><h2>帖子时间分布（最近 14 个有数据日期）</h2>__TIMELINE_CHART__</section>
<section class='panel' style='margin-top:14px'><h2>高频作者</h2><table><thead><tr><th>作者</th><th>帖子数</th><th>占比</th></tr></thead><tbody>__AUTHOR_ROWS__</tbody></table><p class='empty'>分组明细同时输出为 country_summary.csv、keyword_summary.csv 和 author_summary.csv。</p></section>
</main></div></div><script>
let dataset=__DATASET__,filtered=[];const runId=__RUN_ID_JSON__,totalExpected=__DEDUP_COUNT__;let page=1,pageSize=100;
const byId=id=>document.getElementById(id),q=byId('search'),authorSearch=byId('authorSearch'),locationStatus=byId('locationStatus'),country=byId('country'),keyword=byId('keyword'),dateFrom=byId('dateFrom'),dateTo=byId('dateTo'),sortOrder=byId('sortOrder'),pageSizeControl=byId('pageSize'),rows=byId('rows'),visible=byId('visibleCount');
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function locationKey(r){if(r.account_based_in)return String(r.account_based_in).toLowerCase();if(r.account_lookup_status==='unknown')return'__unavailable__';if(r.account_lookup_status==='failed')return'__failed__';return'__pending__'}
function locationLabel(r){if(r.account_based_in)return String(r.account_based_in);if(r.account_lookup_status==='unknown')return'X 未公开';if(r.account_lookup_status==='failed')return'查询失败';return'待查询'}
function render(){const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(Math.max(1,page),pages);const start=(page-1)*pageSize;rows.innerHTML=filtered.slice(start,start+pageSize).map(r=>`<tr data-country="${esc(locationKey(r))}" data-keywords="${esc(String(r.matched_keywords||'').toLowerCase())}" data-date="${esc(String(r.posted_at||'').slice(0,10))}"><td>${esc(r.matched_keywords)}</td><td class="post">${esc(r.post_text)}</td><td>${esc(r.posted_at)}</td><td>@${esc(r.author_username)}</td><td>${esc(locationLabel(r))}</td><td>${esc(r.post_id)}</td><td><a class="plain" target="_blank" rel="noopener" href="${esc(r.post_url)}">打开</a></td></tr>`).join('');visible.textContent=`筛选结果 ${filtered.length} / ${dataset.length} 条`;byId('filteredCount').textContent=filtered.length;byId('filteredAuthors').textContent=new Set(filtered.map(r=>String(r.author_username||'').toLowerCase()).filter(Boolean)).size;byId('filteredCountries').textContent=new Set(filtered.filter(r=>r.account_based_in).map(r=>String(r.account_based_in).toLowerCase())).size;byId('pageInfo').textContent=`第 ${page} / ${pages} 页 · 每页 ${pageSize} 条`;byId('prevPage').disabled=page<=1;byId('nextPage').disabled=page>=pages}
function apply(){const term=q.value.trim().toLowerCase(),author=authorSearch.value.trim().replace(/^@/,'').toLowerCase(),status=locationStatus.value,c=country.value,k=keyword.value,from=dateFrom.value,to=dateTo.value;pageSize=Number(pageSizeControl.value)||100;filtered=dataset.filter(r=>{const d=String(r.posted_at||'').slice(0,10),hay=[r.post_text,r.matched_keywords].join(' ').toLowerCase(),authorName=String(r.author_username||'').toLowerCase(),loc=locationKey(r);return(!term||hay.includes(term))&&(!author||authorName.includes(author))&&(!status||(status==='known'?Boolean(r.account_based_in):loc===status))&&(!c||loc===c)&&(!k||String(r.matched_keywords||'').toLowerCase().split(' | ').includes(k))&&(!from||!d||d>=from)&&(!to||!d||d<=to)});const order=sortOrder.value,cmpText=(a,b)=>String(a||'').localeCompare(String(b||''),'zh-CN',{sensitivity:'base'});filtered.sort((a,b)=>order==='oldest'?cmpText(a.posted_at,b.posted_at):order==='author'?cmpText(a.author_username,b.author_username):order==='country'?cmpText(locationLabel(a),locationLabel(b)):cmpText(b.posted_at,a.posted_at));const active=[term,author,status,c,k,from,to].filter(Boolean).length;byId('activeFilterSummary').textContent=active?`已启用 ${active} 个筛选条件；可导出当前结果`:'当前显示全部去重帖子';page=1;render()}
for(const control of[q,authorSearch])control.addEventListener('input',apply);for(const control of[locationStatus,country,keyword,dateFrom,dateTo,sortOrder,pageSizeControl])control.addEventListener('change',apply);byId('reset').onclick=()=>{q.value='';authorSearch.value='';locationStatus.value='';country.value='';keyword.value='';dateFrom.value='';dateTo.value='';sortOrder.value='newest';pageSizeControl.value='100';apply()};byId('prevPage').onclick=()=>{page--;render()};byId('nextPage').onclick=()=>{page++;render()};
function csvCell(v){const s=String(v??'');return '"'+s.replaceAll('"','""')+'"'}byId('filteredExport').onclick=()=>{const fields=['post_id','post_url','post_text','posted_at','author_username','author_user_id','account_based_in','matched_keywords','account_lookup_status'];const csv='\ufeff'+[fields.join(','),...filtered.map(r=>fields.map(f=>csvCell(r[f])).join(','))].join('\r\n');const a=document.createElement('a'),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.href=url;a.download='filtered_posts.csv';a.style.display='none';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)};
async function loadRuns(){const select=byId('runSwitcher');try{const response=await fetch('/api/history'),data=await response.json();select.innerHTML=data.runs.map(r=>`<option value="${esc(r.run_id)}" ${r.run_id===runId?'selected':''}>${esc(r.run_id)} · ${esc((r.keywords||[]).join('、')||'无关键词')} · ${r.post_count} 条 · ${r.status==='completed'?'已完成':'未完成'}</option>`).join('');select.onchange=()=>{if(select.value&&select.value!==runId)location.href='/results/'+encodeURIComponent(select.value)+'/index.html'}}catch{select.innerHTML=`<option value="${esc(runId)}">批次 ${esc(runId)}</option>`}}async function init(){if(!dataset.length&&totalExpected){visible.textContent='正在载入帖子数据…';try{const response=await fetch('research_dataset.json');dataset=await response.json()}catch{visible.textContent='帖子数据载入失败，请返回采集工作台重试';return}}loadRuns();apply()}init();
</script></body></html>"""
    replacements = {
        "__UPDATED__": html.escape(now_iso()),
        "__DEDUP_COUNT__": str(len(deduped)), "__RAW_COUNT__": str(len(posts)),
        "__AUTHOR_COUNT__": str(len(authors)), "__KNOWN_RATE__": str(known_rate),
        "__KNOWN_COUNT__": str(known), "__COUNTRY_CHART__": bar_chart_svg(countries, "country", "账号所在地分布"),
        "__SLICE_COUNT__": str(len(coverage)), "__EXHAUSTED_COUNT__": str(exhausted_count), "__TRUNCATED_COUNT__": str(truncated_count), "__COVERAGE_ROWS__": coverage_rows,
        "__KEYWORD_CHART__": bar_chart_svg(keywords, "keyword", "关键词命中分布"),
        "__TIMELINE_CHART__": timeline_chart_svg(dates),
        "__COUNTRY_OPTIONS__": country_options, "__KEYWORD_OPTIONS__": keyword_options,
        "__AUTHOR_ROWS__": author_rows, "__DATASET__": embedded,
        "__RUN_ID_JSON__": json.dumps(run_id, ensure_ascii=False), "__RUN_ID__": html.escape(run_id),
        "__LOCATION_NOTICE__": html.escape(location_notice),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


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
    (output_dir / "raw.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_dir / "posts.csv", posts)
    write_csv(output_dir / "posts_deduplicated.csv", deduped, DEDUP_FIELDS)
    write_csv(output_dir / "research_coding_template.csv", deduped, CODING_FIELDS)
    write_csv(output_dir / "country_summary.csv", countries, ["country", "count", "percent"])
    write_csv(output_dir / "keyword_summary.csv", keywords, ["keyword", "count", "percent"])
    write_csv(output_dir / "author_summary.csv", authors, ["author", "count", "percent"])
    write_csv(output_dir / "posted_date_summary.csv", dates, ["date", "count", "percent"])
    write_csv(output_dir / "coverage_report.csv", coverage, ["keyword", "keyword_index", "query", "slice_start", "slice_end", "count", "scrolls_used", "stop_reason", "coverage_status", "completed_at"])
    (output_dir / "research_dataset.json").write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "index.html").write_text(report_html(posts, payload, output_dir.name), encoding="utf-8")
    status = {
        "keyword_count": len(payload.get("requested_keywords", [])),
        "completed_keywords": len(payload.get("search_runs", [])),
        "post_count": len(posts),
        "resolved_authors": len(payload.get("authors", {})),
        "search_slice_count": len(coverage),
        "apparently_exhausted_slices": sum(run.get("coverage_status") == "apparently_exhausted" for run in coverage if isinstance(run, dict)),
        "truncated_slices": sum(run.get("coverage_status") == "truncated" for run in coverage if isinstance(run, dict)),
        "finalized": bool(payload.get("finalized", False)),
        "updated_at": now_iso(),
    }
    (output_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    export_names = [
        "posts.csv", "posts_deduplicated.csv", "research_coding_template.csv",
        "country_summary.csv", "keyword_summary.csv", "author_summary.csv", "posted_date_summary.csv", "coverage_report.csv",
        "research_dataset.json", "raw.json", "status.json", "index.html",
    ]
    with zipfile.ZipFile(output_dir / "all_exports.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in export_names:
            path = output_dir / name
            if path.exists():
                archive.write(path, arcname=name)


def collect_search(page, keyword: str, keyword_index: int, limit: int, max_scrolls: int, render_wait_ms: int, output_dir: Path, start_date: date | None = None, end_date: date | None = None, search_type: str = "live", no_growth_stop: int = 5, scroll_delay_min: float = 3, scroll_delay_max: float = 6, timezone_offset: float = 8) -> tuple[list[dict[str, object]], dict[str, object]]:
    query = build_search_query(keyword, start_date, end_date)
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
        "coverage_status": "apparently_exhausted" if stop_reason == "no_new_results" else "truncated",
        "date_timezone": f"UTC{timezone_offset:+g}",
    }
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
    return _author_from_about(username, payload)


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
        headers = {name: response.header_value(name) or "" for name in ("retry-after", "x-rate-limit-reset", "x-rate-limit-remaining")}
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
    return _author_from_about(username, payload)


def enrich_posts(posts: list[dict[str, object]], author: dict[str, object]) -> None:
    username = str(author.get("username", "")).casefold()
    for post in posts:
        if str(post.get("author_username", "")).casefold() != username:
            continue
        post["author_user_id"] = author.get("user_id", "")
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
            pause = random.uniform(args.author_break_min, args.author_break_max)
            print(f"[作者定期休息] 已处理 {index - 1} 位，暂停 {pause / 60:.1f} 分钟。", flush=True)
            time.sleep(pause)
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
        authors[username.casefold()] = author
        enrich_posts(posts, author)
        save_outputs(output_dir, payload)
        if any(marker in str(author.get("error", "")) for marker in ("HTTP 401", "HTTP 403", "HTTP 429")):
            raise RuntimeError(str(author["error"]))
    payload["active_phase"] = "searching"
    save_outputs(output_dir, payload)
    return len(pending)


def collect(args: argparse.Namespace, keywords: list[str], output_dir: Path) -> None:
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    raw_path = output_dir / "raw.json"
    if args.resume and raw_path.exists():
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        if payload.get("requested_keywords") != keywords:
            raise RuntimeError("断点关键词列表与当前输入不一致")
    else:
        payload = {"requested_keywords": keywords, "search_runs": [], "posts": [], "authors": {}, "finalized": False, "generated_at": now_iso()}
    payload["search_conditions"] = {
        "start_date": args.start_date or "", "end_date": args.end_date or "", "search_type": args.search_type,
        "slice_days": args.slice_days, "no_growth_stop": args.no_growth_stop, "timezone_offset": args.timezone_offset,
        "author_strategy": args.author_strategy,
    }
    posts: list[dict[str, object]] = list(payload.get("posts", []))
    authors: dict[str, dict[str, object]] = dict(payload.get("authors", {}))
    payload["posts"], payload["authors"] = posts, authors
    author_cache = load_author_cache(output_dir)
    cached_count = apply_author_cache(posts, authors, author_cache)
    if cached_count:
        print(f"已复用历史批次作者所在地：{cached_count} 位。", flush=True)
    save_outputs(output_dir, payload)

    slices = make_date_slices(start_date, end_date, args.slice_days)
    work_items = [(keyword_index, keyword, slice_start, slice_end) for keyword_index, keyword in enumerate(keywords, 1) for slice_start, slice_end in slices]
    completed = {
        (str(run.get("keyword", "")), str(run.get("slice_start", "")), str(run.get("slice_end", "")))
        for run in payload.get("search_runs", [])
    }
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
            save_outputs(output_dir, payload)
            last_error: Exception | None = None
            for attempt in range(args.retry_limit + 1):
                try:
                    rows, coverage = collect_search(page, keyword, keyword_index, args.limit, args.max_scrolls, args.render_wait_ms, output_dir, slice_start, slice_end, args.search_type, args.no_growth_stop, args.scroll_delay_min, args.scroll_delay_max, args.timezone_offset)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt >= args.retry_limit:
                        raise
                    factor = attempt + 1
                    if "429" in str(exc) or "限流" in str(exc):
                        factor *= 2
                    pause = random.uniform(args.backoff_min, args.backoff_max) * factor
                    print(f"[退避重试 {attempt + 1}/{args.retry_limit}] {type(exc).__name__}: {exc}；暂停 {pause / 60:.1f} 分钟。", flush=True)
                    time.sleep(pause)
            if last_error is not None:
                raise last_error
            existing_keys = {(str(p.get("keyword", "")), str(p.get("post_id", ""))) for p in posts}
            for row in rows:
                key = (keyword, str(row.get("post_id", "")))
                if key not in existing_keys:
                    posts.append(row); existing_keys.add(key)
            cached_count = apply_author_cache(posts, authors, author_cache)
            if cached_count:
                print(f"  复用历史批次作者所在地：{cached_count} 位。", flush=True)
            payload["search_runs"].append({"keyword": keyword, "keyword_index": keyword_index, **coverage, "completed_at": now_iso()})
            save_outputs(output_dir, payload)
            print(f"  搜索片完成：{len(rows)} 条，停止原因 {coverage['stop_reason']}，断点已保存。", flush=True)
            if args.author_strategy == "per_slice":
                slice_authors = list(dict.fromkeys(str(row.get("author_username", "")) for row in rows if row.get("author_username")))
                enrich_author_batch(page, args, output_dir, payload, posts, authors, slice_authors, f"日期片 {task_key[1]}—{task_key[2]} 作者", direct_state)
            completed_this_run += 1

        unique_authors = list(dict.fromkeys(str(p.get("author_username", "")) for p in posts if p.get("author_username")))
        enrich_author_batch(page, args, output_dir, payload, posts, authors, unique_authors, "任务收尾作者", direct_state)

        payload["finalized"] = True
        payload["active_phase"] = "completed"
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
    parser.add_argument("--author-strategy", choices=["per_slice", "after_search"], default="per_slice", help="每个日期片后查询新增作者，或全部搜索后统一查询")
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
        if not 0 <= args.slice_days <= 3660 or not 2 <= args.no_growth_stop <= 50:
            raise ValueError("日期切片或无新增停止次数无效")
        if not -12 <= args.timezone_offset <= 14:
            raise ValueError("时区偏移必须在 UTC-12 到 UTC+14 之间")
        if (
            not 0 <= args.delay_min <= args.delay_max
            or not 2 <= args.author_delay_min <= args.author_delay_max
            or not 1 <= args.direct_author_delay_min <= args.direct_author_delay_max <= 300
        ):
            raise ValueError("等待区间无效")
        if not 0 <= args.author_break_every <= 10000 or not 0 <= args.author_break_min <= args.author_break_max <= 86400:
            raise ValueError("作者定期休息参数无效")
        if not 0 <= args.author_429_retries <= 10 or not 60 <= args.author_429_base_cooldown <= args.author_429_max_cooldown <= 86400:
            raise ValueError("作者 429 冷却参数无效")
        if not 1 <= args.scroll_delay_min <= args.scroll_delay_max <= 300:
            raise ValueError("滚动等待区间无效")
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
        print(f"每个搜索片目标：{args.limit if args.limit > 0 else '不限量'}", flush=True)
        print(f"日期切片：{str(args.slice_days) + ' 天/片' if args.slice_days > 0 else '不切片'}", flush=True)
        print(f"输出目录：{args.output.resolve()}", flush=True)
        if args.dry_run:
            print("DRY RUN：输入校验通过，未访问 X。", flush=True)
            return 0
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
