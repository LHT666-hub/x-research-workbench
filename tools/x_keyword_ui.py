from __future__ import annotations

import argparse
import csv
import io
import json
import mimetypes
import os
import signal
import subprocess
import threading
import urllib.parse
import webbrowser
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
COLLECTOR = ROOT / "tools" / "x_keyword_collector.py"
OUTPUT_ROOT = ROOT / "outputs" / "x_keyword_runs"
MAX_KEYWORDS = 500
ACCOUNT_CONFIG_PATH = ROOT / "tmp" / "x-account-nodes.json"
DEFAULT_ACCOUNT_LABELS = {
    "a": "账号 A（现有 GPT 会话）",
    "b": "账号 B（独立 Claude 会话）",
    "c": "账号 C（独立研究会话）",
    "d": "账号 D（独立研究会话）",
}


def session_directory(node_id: str) -> Path:
    suffix = "" if node_id == "a" else f"-{node_id}"
    return ROOT / "tmp" / f"chrome-x-keyword-profile{suffix}"


def load_account_nodes() -> dict[str, dict[str, Any]]:
    stored: object = {}
    try:
        stored = json.loads(ACCOUNT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    rows = stored.get("accounts", []) if isinstance(stored, dict) else []
    nodes: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            node_id = str(row.get("id", "")).strip().lower()
            label = str(row.get("label", "")).strip()
            if node_id and node_id.replace("_", "").isalnum() and label:
                nodes[node_id] = {
                    "label": label[:80],
                    "active": bool(row.get("active", True)),
                    "session_dir": session_directory(node_id),
                }
    if not nodes:
        nodes = {
            key: {"label": label, "active": True, "session_dir": session_directory(key)}
            for key, label in DEFAULT_ACCOUNT_LABELS.items()
        }
    return nodes


ACCOUNT_NODES = load_account_nodes()
ACCOUNT_LOCK = threading.RLock()


def normalize_node(value: object, require_active: bool = False) -> str:
    node = str(value or "a").strip().lower()
    if node not in ACCOUNT_NODES or (require_active and not ACCOUNT_NODES[node].get("active", True)):
        raise ValueError("采集账号节点无效")
    return node


def node_label(node_id: str) -> str:
    return str(ACCOUNT_NODES.get(node_id, {}).get("label") or f"账号 {node_id.upper()}")


def save_account_nodes() -> None:
    ACCOUNT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": node_id, "label": str(node["label"]), "active": bool(node.get("active", True))}
        for node_id, node in ACCOUNT_NODES.items()
    ]
    temporary = ACCOUNT_CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"accounts": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(ACCOUNT_CONFIG_PATH)


def next_node_id() -> str:
    index = 0
    while True:
        value, number = "", index
        while True:
            value = chr(97 + number % 26) + value
            number = number // 26 - 1
            if number < 0:
                break
        if value not in ACCOUNT_NODES:
            return value
        index += 1


def run_directory(run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id:
        raise ValueError("历史任务编号无效")
    target = (OUTPUT_ROOT / run_id).resolve()
    if not target.is_relative_to(OUTPUT_ROOT.resolve()) or not target.is_dir():
        raise ValueError("历史任务不存在")
    return target


def history_rows() -> list[dict[str, Any]]:
    if not OUTPUT_ROOT.exists():
        return []
    rows: list[dict[str, Any]] = []
    for directory in sorted((path for path in OUTPUT_ROOT.iterdir() if path.is_dir()), reverse=True):
        config = read_json(directory / "run_config.json", {})
        status = read_json(directory / "status.json", {})
        raw = read_json(directory / "raw.json", {})
        if not isinstance(config, dict):
            config = {}
        if not isinstance(status, dict):
            status = {}
        if not isinstance(raw, dict):
            raw = {}
        keywords = raw.get("requested_keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        posts = [row for row in raw.get("posts", []) if isinstance(row, dict)]
        authors_map = raw.get("authors", {})
        authors_map = authors_map if isinstance(authors_map, dict) else {}
        unique_authors = len({str(post.get("author_username", "")).casefold() for post in posts if post.get("author_username")})
        known_locations = sum(bool(author.get("account_based_in")) for author in authors_map.values() if isinstance(author, dict))
        finalized = bool(raw.get("finalized", False))
        rows.append({
            "run_id": directory.name,
            "created_at": raw.get("generated_at", directory.name),
            "completed_at": raw.get("completed_at", ""),
            "status": "completed" if finalized else "incomplete",
            "keywords": keywords,
            "keyword_count": len(keywords) or int(config.get("keyword_count", 0) or 0),
            "post_count": int(status.get("post_count", 0) or 0),
            "resolved_authors": int(status.get("resolved_authors", 0) or 0),
            "unique_authors": unique_authors,
            "known_locations": known_locations,
            "start_date": str(config.get("start_date", "")),
            "end_date": str(config.get("end_date", "")),
            "timezone_offset": float(config.get("timezone_offset", 0) or 0),
            "data_quality_warning": str(config.get("data_quality_warning", "")),
            "search_type": str(config.get("search_type", "live")),
            "slice_days": int(config.get("slice_days", 0) or 0),
            "no_growth_stop": int(config.get("no_growth_stop", 5) or 5),
            "author_strategy": str(config.get("author_strategy", "per_slice")),
            "account_node": normalize_node(config.get("account_node", "a")),
            "account_label": node_label(normalize_node(config.get("account_node", "a"))),
            "pace": str(config.get("pace", "balanced")),
            "limit": int(config.get("limit", 0) or 0),
            "max_scrolls": int(config.get("max_scrolls", 12) or 12),
            "result_url": "/results/" + urllib.parse.quote(directory.name) + "/index.html" if (directory / "index.html").exists() else "",
            "zip_url": "/results/" + urllib.parse.quote(directory.name) + "/all_exports.zip" if (directory / "all_exports.zip").exists() else "",
            "can_resume": bool(raw) and not finalized and not bool(config.get("data_quality_warning")),
        })
    return rows


def cumulative_posts() -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    keyword_lists: dict[str, list[str]] = {}
    for run in reversed(history_rows()):
        dataset = read_json(run_directory(str(run["run_id"])) / "research_dataset.json", [])
        if not isinstance(dataset, list):
            continue
        for index, item in enumerate(dataset):
            if not isinstance(item, dict):
                continue
            identity = str(item.get("post_id") or item.get("post_url") or f"{run['run_id']}-{index}")
            if identity not in merged:
                merged[identity] = dict(item)
                keyword_lists[identity] = []
            for keyword in str(item.get("matched_keywords", "")).split(" | "):
                keyword = keyword.strip()
                if keyword and keyword.casefold() not in {value.casefold() for value in keyword_lists[identity]}:
                    keyword_lists[identity].append(keyword)
            current = merged[identity]
            for field in ("author_user_id", "account_based_in", "author_display_name"):
                if not current.get(field) and item.get(field):
                    current[field] = item[field]
    output = []
    for identity, item in merged.items():
        item["matched_keywords"] = " | ".join(keyword_lists[identity])
        item["keyword_count"] = len(keyword_lists[identity])
        output.append(item)
    return output


def rows_csv(rows: list[dict[str, object]]) -> bytes:
    fields = ["post_id", "post_url", "post_text", "posted_at", "author_username", "author_display_name", "author_user_id", "account_based_in", "matched_keywords", "keyword_count", "source", "account_lookup_status", "collected_at"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def run_details(run_id: str) -> dict[str, Any]:
    directory = run_directory(run_id)
    config = read_json(directory / "run_config.json", {})
    status = read_json(directory / "status.json", {})
    raw = read_json(directory / "raw.json", {})
    config = config if isinstance(config, dict) else {}
    status = status if isinstance(status, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    posts = [row for row in raw.get("posts", []) if isinstance(row, dict)]
    authors_map = raw.get("authors", {})
    authors_map = authors_map if isinstance(authors_map, dict) else {}
    authors = [row for row in authors_map.values() if isinstance(row, dict)]
    coverage = [row for row in raw.get("search_runs", []) if isinstance(row, dict)]
    requested = [str(value) for value in raw.get("requested_keywords", [])]
    unique_authors = len({str(post.get("author_username", "")).casefold() for post in posts if post.get("author_username")})
    start_text, end_text = str(config.get("start_date", "")), str(config.get("end_date", ""))
    slice_days = int(config.get("slice_days", 0) or 0)
    expected_slices = len(requested)
    if start_text and end_text and slice_days > 0:
        start_value, end_value = date.fromisoformat(start_text), date.fromisoformat(end_text)
        days = (end_value - start_value).days + 1
        expected_slices = len(requested) * ((days + slice_days - 1) // slice_days)
    finalized = bool(raw.get("finalized", False))
    node = normalize_node(config.get("account_node", "a"))
    manager = globals().get("MANAGERS", {}).get(node)
    is_running = bool(
        manager
        and getattr(manager, "status", "") == "running"
        and getattr(manager, "output_dir", None) == directory
    )
    active_phase = str(raw.get("active_phase", ""))
    if finalized:
        phase = "completed"
    elif is_running and active_phase == "enriching_authors":
        phase = "enriching_authors"
    elif len(coverage) < expected_slices:
        phase = "searching" if is_running else "search_paused"
    else:
        phase = "enriching_authors" if is_running else "author_lookup_paused"
    log_tail = ""
    try:
        log_tail = (directory / "collector.log").read_text(encoding="utf-8", errors="replace")[-20_000:]
    except OSError:
        pass
    return {
        "run_id": run_id, "config": config, "status": status, "keywords": requested,
        "account_node": node, "account_label": node_label(node),
        "phase": phase, "is_running": is_running, "finalized": finalized, "posts_count": len(posts),
        "posts_preview": posts[-200:], "coverage": coverage,
        "expected_slices": expected_slices, "completed_slices": len(coverage),
        "unique_authors": unique_authors, "resolved_authors": len(authors),
        "known_locations": sum(bool(author.get("account_based_in")) for author in authors),
        "authors": authors[-200:], "log_tail": log_tail,
        "result_url": "/results/" + urllib.parse.quote(run_id) + "/index.html" if (directory / "index.html").exists() else "",
        "zip_url": "/results/" + urllib.parse.quote(run_id) + "/all_exports.zip" if (directory / "all_exports.zip").exists() else "",
        "can_resume": bool(raw) and not finalized,
    }


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>X关键词帖子采集器</title><style>
:root{--navy:#152637;--blue:#1677ff;--line:#d8e2eb;--muted:#687b8d;--bg:#f3f6f9}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:"Microsoft YaHei",system-ui,sans-serif;color:#172033}main{max-width:1240px;margin:auto;padding:25px}.hero{background:linear-gradient(120deg,#0f1c28,#244b68);color:white;border-radius:15px;padding:25px 29px;margin-bottom:18px}.hero h1{margin:0 0 8px;font-size:28px}.hero p{margin:0;color:#dce8f1;line-height:1.6}.grid{display:grid;grid-template-columns:1.08fr .92fr;gap:18px}@media(max-width:900px){.grid{grid-template-columns:1fr}}.panel{background:white;border:1px solid var(--line);border-radius:12px;padding:20px}.panel h2{margin:0 0 13px;font-size:19px}.step{color:var(--blue);font-size:13px;margin-right:5px}.sub{color:var(--muted);font-size:13px;line-height:1.6}textarea{width:100%;min-height:285px;resize:vertical;padding:13px;border:1px solid #aebdca;border-radius:8px;font:14px/1.55 Consolas,"Microsoft YaHei",monospace}textarea:focus,input:focus,select:focus,button:focus-visible{outline:2px solid #a8ccff;outline-offset:2px;border-color:var(--blue)}.row{display:flex;gap:11px;align-items:center;flex-wrap:wrap;margin-top:11px}.row label{display:flex;align-items:center;gap:6px;font-size:14px}input[type=number],input[type=date],input[type=text],select{padding:8px 9px;border:1px solid #aebdca;border-radius:7px;background:white}.buttons{display:flex;gap:9px;flex-wrap:wrap;margin-top:15px}button,.result{border:0;border-radius:8px;padding:10px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.primary{background:var(--blue);color:white}.secondary{background:#e6eef5;color:#17324d}.danger{background:#ffe8e8;color:#a11}.success{background:#e1f5e9;color:#116d3d}button:disabled{opacity:.48;cursor:not-allowed}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:13px 0}.stat{background:#f1f6fa;border-radius:8px;padding:11px;text-align:center}.stat b{display:block;font-size:21px;color:#174c73}.stat span{font-size:12px;color:var(--muted)}.status{display:flex;align-items:center;gap:9px;padding:11px 13px;border-radius:8px;background:#eef4f8;margin-bottom:12px}.dot{width:10px;height:10px;border-radius:50%;background:#8aa0b3}.dot.running{background:#1677ff;box-shadow:0 0 0 5px #dcecff}.dot.success{background:#1a9b53}.dot.failed{background:#d73737}.dot.stopped{background:#e18a16}pre{height:445px;overflow:auto;background:#101820;color:#d8e7f2;padding:14px;border-radius:9px;white-space:pre-wrap;word-break:break-word;font:13px/1.55 Consolas,monospace}.warn{background:#fff8df;border:1px solid #f0d993;color:#7c5700;padding:10px 12px;border-radius:8px;font-size:13px;line-height:1.55;margin-top:12px}.hidden{display:none!important}.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:11px 0}.flow span{background:#edf4fa;border:1px solid #d4e3ef;border-radius:8px;padding:9px 6px;text-align:center;font-size:12px}.fields{display:flex;flex-wrap:wrap;gap:6px}.fields span{background:#edf4fa;border:1px solid #d4e3ef;border-radius:14px;padding:5px 8px;font:12px Consolas,monospace}.history{margin-top:18px}.history-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.history-table{overflow:auto;border:1px solid var(--line);border-radius:9px;max-height:430px}.history table{border-collapse:collapse;width:100%;font-size:13px}.history th,.history td{padding:10px;border-bottom:1px solid #e4ebf0;text-align:left;vertical-align:top}.history th{background:#17324d;color:white;position:sticky;top:0}.tag{display:inline-block;padding:3px 7px;border-radius:12px;background:#e8eef3;font-size:11px}.tag.ok{background:#e1f5e9;color:#116d3d}.mini{padding:6px 9px;font-size:12px}.preset{display:grid;grid-template-columns:1fr 1fr auto auto;gap:8px;margin-top:10px}.detail-kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin:14px 0}.progress-track{height:10px;background:#e5edf3;border-radius:8px;overflow:hidden}.progress-fill{height:100%;background:var(--blue)}.tabs{display:flex;gap:7px;border-bottom:1px solid var(--line);margin:15px 0}.tabs button{background:transparent;color:var(--muted);border-radius:0}.tabs button.active{color:var(--blue);border-bottom:3px solid var(--blue)}.tab-pane{display:none}.tab-pane.active{display:block}.detail-table{overflow:auto;max-height:520px;border:1px solid var(--line);border-radius:8px}.detail-table table{border-collapse:collapse;width:100%;font-size:12px}.detail-table th,.detail-table td{padding:9px;border-bottom:1px solid #e7edf2;text-align:left;vertical-align:top}.detail-table th{position:sticky;top:0;background:#17324d;color:white}.post-cell{min-width:360px;white-space:normal}.phase{padding:12px;border-radius:8px;background:#eef4f8}.node-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin-top:12px}.account-manager{margin-top:14px;padding:14px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}.account-row{display:grid;grid-template-columns:minmax(180px,1fr) auto auto;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #e4ebf0}.account-row:last-child{border-bottom:0}@media(max-width:900px){.detail-kpis{grid-template-columns:repeat(2,1fr)}}@media(max-width:700px){.preset{grid-template-columns:1fr 1fr}.history-head{align-items:flex-start;flex-direction:column}.account-row{grid-template-columns:1fr}}</style></head><body><main>
<style>
:root{--navy:#2d2926;--blue:#c86445;--line:#ded8ce;--muted:#746e66;--bg:#f3f0e8;--surface:#fbfaf6;--surface-strong:#fffdf9;--soft:#eee9df;--success:#287a55;--danger:#a8483b;--shadow:0 18px 50px rgba(62,48,35,.08)}
body{background:radial-gradient(circle at 12% 0%,rgba(214,143,105,.12),transparent 29rem),linear-gradient(180deg,#f7f4ed 0,#f1ede4 100%);background-attachment:fixed;color:#312d29}main{max-width:1380px;padding:30px}.hero{position:relative;overflow:hidden;background:linear-gradient(135deg,#352f2b 0%,#241f1c 100%);color:#fffaf2;border:1px solid rgba(255,255,255,.12);border-radius:24px;padding:34px 38px;margin-bottom:20px;box-shadow:var(--shadow)}.hero:after{content:"";position:absolute;width:280px;height:280px;border-radius:50%;right:-70px;top:-145px;background:radial-gradient(circle,rgba(216,116,80,.55),transparent 68%)}.hero h1{position:relative;font:600 34px/1.15 Georgia,"Songti SC",serif;letter-spacing:-.02em}.hero p{position:relative;color:#ded7cd;line-height:1.7;max-width:850px}.grid{grid-template-columns:minmax(0,1.08fr) minmax(360px,.92fr);gap:20px}.panel{background:rgba(255,253,249,.9);backdrop-filter:blur(14px);border:1px solid rgba(205,195,182,.78);border-radius:20px;padding:24px;box-shadow:0 8px 30px rgba(62,48,35,.055)}.panel h2{font:600 20px/1.3 Georgia,"Songti SC",serif}.step{display:inline-flex;align-items:center;justify-content:center;color:#974329;background:#f3dfd5;border-radius:999px;font:700 11px/1 system-ui;padding:6px 8px;margin-right:8px;letter-spacing:.06em}.sub{color:var(--muted)}textarea,input[type=number],input[type=date],input[type=text],select{background:#fffefa;border-color:#cfc7bc}textarea{border-radius:13px;padding:15px;box-shadow:inset 0 1px 2px rgba(50,40,30,.04)}button,.result{border-radius:11px;transition:transform .16s ease,box-shadow .16s ease,background .16s ease}button:hover:not(:disabled),.result:hover{transform:translateY(-1px);box-shadow:0 7px 18px rgba(66,47,34,.11)}.primary{background:#c86445}.secondary{background:#ece7de;color:#3e3934}.danger{background:#f3dfda;color:#963f34}.success{background:#dfece3;color:#246747}.status,.stat,.flow span,.phase{background:#f0ece4}.stat b{color:#4a4039}.warn{background:#fbf0d8;border-color:#e8cf9b;color:#6e5427}.node-grid{grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.account-manager{background:#f6f2ea;border-radius:15px}.history-table,.detail-table{border-radius:13px}.history th,.detail-table th{background:#37312d}.tabs button.active{color:#a84e33;border-bottom-color:#c86445}.progress-fill{background:#c86445}pre{background:#292724;color:#e9e3d8;border:1px solid #413d38;border-radius:14px}.bulk-bar{display:grid;grid-template-columns:repeat(3,minmax(120px,1fr));gap:10px;margin-top:14px}.bulk-kpi{padding:12px 14px;border-radius:13px;background:#f0ece4;border:1px solid #e2dbcf}.bulk-kpi b{display:block;font:600 22px/1.2 Georgia,serif}.bulk-kpi span{font-size:12px;color:var(--muted)}@media(max-width:980px){.grid{grid-template-columns:1fr}}@media(max-width:700px){main{padding:14px}.hero{padding:26px 22px;border-radius:18px}.hero h1{font-size:28px}.bulk-bar{grid-template-columns:1fr}.panel{padding:18px;border-radius:16px}}
</style>
<style>
.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}.setting-group{padding:16px;border:1px solid var(--line);border-radius:15px;background:#f8f5ee}.setting-group h3{margin:0 0 4px;font:600 16px/1.3 Georgia,"Songti SC",serif}.setting-group>p{margin:0 0 13px}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}.field{display:flex!important;align-items:stretch!important;gap:6px!important;flex-direction:column;font-size:12px!important;color:var(--muted)}.field>span:first-child{font-weight:700;color:#4a443e}.field input,.field select{min-width:0;width:100%;height:40px}.range-inputs{display:grid;grid-template-columns:1fr auto 1fr;gap:6px;align-items:center}.advanced-card{margin-top:12px;border:1px solid var(--line);border-radius:15px;background:#f8f5ee;overflow:hidden}.advanced-card summary{cursor:pointer;padding:14px 16px;font-weight:700;color:#514940}.advanced-card[open] summary{border-bottom:1px solid var(--line)}.advanced-card .advanced-body{padding:15px}.advanced-card .field-grid{grid-template-columns:repeat(3,minmax(0,1fr))}@media(max-width:900px){.settings-grid{grid-template-columns:1fr}.advanced-card .field-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){.field-grid,.advanced-card .field-grid{grid-template-columns:1fr}}
</style>
<section class="hero"><h1>X Research Workbench</h1><p>关键词检索、日期切片、作者所在地补全与研究数据导出，都集中在一个安静、可追踪的工作台里。</p></section>
<section class="panel" data-od-id="account-control" style="margin-bottom:20px"><div class="history-head"><div><h2><span class="step">总控</span>多账号采集队列</h2><p class="sub">每个节点使用独立登录会话。总控操作只影响启用的节点，并保留每个任务的断点。</p></div><div class="buttons"><button class="primary" id="showAccountManager" aria-expanded="false">＋ 添加/管理账号</button><button class="danger" id="stopAll">一键全部断点</button><button class="success" id="resumeAll">一键全部续爬</button><button class="secondary" id="refreshNodes">刷新</button></div></div><div class="bulk-bar" aria-live="polite"><div class="bulk-kpi"><b id="nodeTotal">0</b><span>启用账号</span></div><div class="bulk-kpi"><b id="nodeRunning">0</b><span>正在运行</span></div><div class="bulk-kpi"><b id="nodeResumable">0</b><span>可从断点继续</span></div></div><div id="nodeCards" class="node-grid"></div><div id="accountManager" class="account-manager hidden"><div class="history-head"><div><strong>账号节点管理</strong><p class="sub">名称用于辨认任务；登录 Cookie 只保存在这个节点自己的本地目录。</p></div><button class="secondary mini" id="closeAccountManager">收起</button></div><div class="row"><label for="newAccountName">新账号名称</label><input id="newAccountName" type="text" maxlength="80" placeholder="例如：Claude 英文样本"><button class="primary" id="addAccount">添加账号节点</button></div><div id="accountList" aria-live="polite"></div></div></section>
<div class="grid"><section class="panel"><h2><span class="step">01</span>输入检索词</h2><textarea id="keywords" placeholder="每行一个关键词，例如：&#10;artificial intelligence&#10;ChatGPT&#10;生成式人工智能"></textarea><div class="buttons"><button class="secondary" id="example">载入示例</button><button class="secondary" id="clear">清空</button></div><div class="preset"><input id="presetName" type="text" placeholder="关键词方案名称"><select id="presetList"><option value="">选择已保存方案</option></select><button class="secondary mini" id="savePreset">保存方案</button><button class="secondary mini" id="loadPreset">载入方案</button></div>
<div class="stats"><div class="stat"><b id="keywordCount">0</b><span>去重关键词</span></div><div class="stat"><b id="postEstimate">0</b><span>目标帖子上限</span></div><div class="stat"><b id="estimate">—</b><span>最低预计耗时</span></div></div>
<div class="row"><label>采集账号 <select id="accountNode" aria-label="选择采集账号"></select></label><span class="sub">各节点使用不同浏览器目录，可以同时运行；首次使用新节点时会要求登录。</span></div>
<h2><span class="step">02</span>运行设置</h2><div class="settings-grid"><section class="setting-group"><h3>采集范围</h3><p class="sub">决定搜索排序、每个日期片的目标量与最大滚动预算。</p><div class="field-grid"><label class="field"><span>采集模式</span><select id="mode"><option value="smoke">试水：每片5条</option><option value="formal">正式：每片500条</option><option value="exhaustive">尽量完整：滚到无新增</option><option value="custom">自定义</option></select></label><label class="field"><span>搜索排序</span><select id="searchType"><option value="live">最新帖子</option><option value="top">热门帖子</option></select></label><label class="field"><span>每片目标</span><input id="limit" type="number" min="0" max="100000" value="20" title="0 表示不设条数上限"></label><label class="field"><span>最多滚动</span><input id="scrolls" type="number" min="1" max="10000" value="12"></label></div></section><section class="setting-group"><h3>时间与切片</h3><p class="sub">自然日按所选时区解释，历史研究建议按天切片。</p><div class="field-grid"><label class="field"><span>开始日期</span><input id="startDate" type="date"></label><label class="field"><span>结束日期</span><input id="endDate" type="date"></label><label class="field"><span>日期时区</span><select id="timezoneOffset"><option value="8" selected>北京时间 UTC+8</option><option value="0">UTC</option><option value="9">日本时间 UTC+9</option><option value="-5">美国东部标准时 UTC-5</option><option value="-8">美国西部标准时 UTC-8</option></select></label><label class="field"><span>日期切片</span><select id="sliceDays"><option value="0">不切片</option><option value="1">按天</option><option value="7" selected>按7天</option><option value="30">按30天</option></select></label><label class="field"><span>连续无新增停止</span><div class="range-inputs"><input id="noGrowth" type="number" min="2" max="50" value="6"><span>次</span></div></label></div></section><section class="setting-group"><h3>作者所在地补全</h3><p class="sub">每片完成后补作者，可更早形成包含所在地的可用样本。</p><div class="field-grid"><label class="field"><span>查询策略</span><select id="authorStrategy"><option value="per_slice" selected>每片完成后立即查询新增作者</option><option value="after_search">全部帖子搜索后统一查询</option></select></label><label class="field"><span>查询间隔（秒）</span><div class="range-inputs"><input id="authorMin" type="number" min="2" max="300" value="8"><span>—</span><input id="authorMax" type="number" min="2" max="300" value="15"></div></label></div></section><section class="setting-group"><h3>运行节奏</h3><p class="sub">稳妥档更适合长时间研究任务；429 会自动进入共享冷却。</p><div class="field-grid"><label class="field"><span>节奏档位</span><select id="pace"><option value="safe" selected>稳妥（推荐）</option><option value="balanced">均衡</option><option value="custom">自定义节奏</option></select></label></div></section></div><details id="paceAdvanced" class="advanced-card"><summary>高级节奏参数</summary><div class="advanced-body"><div class="field-grid"><label class="field"><span>滚动等待（秒）</span><div class="range-inputs"><input id="scrollDelayMin" type="number" min="1" max="300" value="4"><span>—</span><input id="scrollDelayMax" type="number" min="1" max="300" value="8"></div></label><label class="field"><span>搜索片间隔（秒）</span><div class="range-inputs"><input id="sliceDelayMin" type="number" min="0" max="3600" value="15"><span>—</span><input id="sliceDelayMax" type="number" min="0" max="3600" value="30"></div></label><label class="field"><span>每完成多少片长休息</span><input id="breakEvery" type="number" min="0" max="10000" value="15"></label><label class="field"><span>长休息（秒）</span><div class="range-inputs"><input id="breakMin" type="number" min="0" max="86400" value="180"><span>—</span><input id="breakMax" type="number" min="0" max="86400" value="360"></div></label><label class="field"><span>失败重试次数</span><input id="retryLimit" type="number" min="0" max="10" value="3"></label><label class="field"><span>退避（秒）</span><div class="range-inputs"><input id="backoffMin" type="number" min="10" max="86400" value="180"><span>—</span><input id="backoffMax" type="number" min="10" max="86400" value="420"></div></label></div></div></details>
<div class="flow"><span>日期自动切片</span><span>滚动到无新增</span><span>帖子与作者去重</span><span>覆盖诊断报告</span></div><div class="warn">X 不显示总命中数，因此“尽量完整”只能表示持续滚动到网页不再返回新帖子，并逐片记录停止原因，不能证明平台数据库绝对全量。首次运行请正常登录 X。</div>
<div class="buttons"><button class="primary" id="start">开始采集</button><button class="danger" id="stop" disabled>停止并保留断点</button><button class="success" id="resume" disabled>继续上次任务</button></div>
<h2 style="margin-top:20px"><span class="step">输出字段</span></h2><div class="fields"><span>keyword</span><span>post_text</span><span>posted_at</span><span>author_username</span><span>author_user_id</span><span>account_based_in</span><span>post_url</span></div><p class="sub">任务完成后同时生成：原始命中、去重样本、国家/关键词/作者分组、内容编码模板、JSON、可视化面板及完整 ZIP。</p>
</section><section class="panel"><h2><span class="step">03</span>运行与结果</h2><div class="status"><span class="dot" id="dot"></span><strong id="status">空闲</strong></div><div id="output" class="sub">尚未开始。</div><div class="buttons"><button class="result primary hidden" id="currentDetail">查看当前任务详情</button><a class="result secondary hidden" id="result" target="_blank">在新标签打开完整结果</a></div><label class="sub" for="log">实时日志</label><pre id="log">等待任务……</pre></section></div><section class="panel history" id="historySection"><div class="history-head"><div><h2><span class="step">04</span>历史研究任务</h2><p class="sub" id="historySummary">正在读取历史记录……</p></div><div class="buttons"><select id="batchPicker" aria-label="选择历史批次"><option value="">选择一个批次</option></select><button class="primary" id="openBatch">查看所选批次</button><a class="result secondary" href="/history/cumulative_posts.csv">导出跨批次累计去重样本</a><button class="secondary" id="refreshHistory">刷新历史</button></div></div><div class="history-table"><table><thead><tr><th>任务时间</th><th>状态</th><th>关键词</th><th>检索日期</th><th>帖子</th><th>作者所在地</th><th>操作</th></tr></thead><tbody id="historyBody"><tr><td colspan="7">正在读取……</td></tr></tbody></table></div></section><section class="panel history hidden" id="runDetail"><div class="history-head"><div><button class="secondary" id="backHistory">← 返回历史任务</button><h2 id="detailTitle" style="margin-top:14px">任务详情</h2><p class="sub" id="detailMeta"></p></div><div class="buttons" id="detailActions"></div></div><div class="phase" id="detailPhase"></div><div class="detail-kpis" id="detailKpis"></div><div class="progress-track"><div class="progress-fill" id="detailProgress"></div></div><div class="tabs"><button class="active" data-tab="postsPane">帖子</button><button data-tab="authorsPane">作者所在地</button><button data-tab="coveragePane">覆盖诊断</button><button data-tab="logsPane">运行日志</button></div><div class="tab-pane active" id="postsPane"></div><div class="tab-pane" id="authorsPane"></div><div class="tab-pane" id="coveragePane"></div><div class="tab-pane" id="logsPane"><pre id="detailLog"></pre></div></section></main>
<script>const el=id=>document.getElementById(id),keywords=el('keywords'),mode=el('mode'),limit=el('limit'),scrolls=el('scrolls'),start=el('start'),stop=el('stop'),resume=el('resume'),result=el('result'),log=el('log');keywords.value=localStorage.getItem('xKeywordInputs')||'';
function parse(){const seen=new Set(),out=[];for(let line of keywords.value.split(/\r?\n/)){line=line.trim();if(!line||line.startsWith('#'))continue;const k=line.toLowerCase();if(!seen.has(k)){seen.add(k);out.push(line)}}return out}function applyMode(){if(mode.value==='smoke'){limit.value=5;scrolls.value=4;el('sliceDays').value=0;el('noGrowth').value=3;limit.disabled=true;scrolls.disabled=true}else if(mode.value==='formal'){limit.value=500;scrolls.value=150;el('sliceDays').value=7;el('noGrowth').value=6;limit.disabled=true;scrolls.disabled=true}else if(mode.value==='exhaustive'){limit.value=0;scrolls.value=1000;el('sliceDays').value=1;el('noGrowth').value=8;limit.disabled=true;scrolls.disabled=true}else{limit.disabled=false;scrolls.disabled=false}update()}function applyPace(){const p=el('pace').value;if(p==='safe'){[['authorMin',8],['authorMax',15],['scrollDelayMin',4],['scrollDelayMax',8],['sliceDelayMin',15],['sliceDelayMax',30],['breakEvery',15],['breakMin',180],['breakMax',360],['retryLimit',3],['backoffMin',180],['backoffMax',420]].forEach(([id,v])=>el(id).value=v)}else if(p==='balanced'){[['authorMin',8],['authorMax',15],['scrollDelayMin',3],['scrollDelayMax',6],['sliceDelayMin',8],['sliceDelayMax',15],['breakEvery',20],['breakMin',120],['breakMax',240],['retryLimit',3],['backoffMin',120],['backoffMax',300]].forEach(([id,v])=>el(id).value=v)}el('paceAdvanced').open=p==='custom';update()}function update(){const n=parse().length,target=Number(limit.value),total=n*target,avg=(Number(el('authorMin').value)+Number(el('authorMax').value))/2;el('keywordCount').textContent=n;el('postEstimate').textContent=target?total:'不限';const sec=n*15+total*avg;el('estimate').textContent=!n?'—':!target?'取决于搜索结果':sec<3600?Math.ceil(sec/60)+'分钟':(sec/3600).toFixed(1)+'小时';localStorage.setItem('xKeywordInputs',keywords.value)}keywords.oninput=update;mode.onchange=applyMode;el('pace').onchange=applyPace;for(const id of ['limit','scrolls','authorMin','authorMax','sliceDays','noGrowth'])el(id).oninput=update;el('example').onclick=()=>{keywords.value='GPT';el('startDate').value='2026-01-01';el('endDate').value=new Date().toISOString().slice(0,10);update()};el('clear').onclick=()=>{if(confirm('清空关键词吗？')){keywords.value='';update()}};
function presets(){try{return JSON.parse(localStorage.getItem('xKeywordPresets')||'{}')}catch{return {}}}function renderPresets(){const data=presets(),select=el('presetList'),current=select.value;select.innerHTML='<option value="">选择已保存方案</option>'+Object.keys(data).sort().map(name=>`<option value="${name.replaceAll('"','&quot;')}">${name}</option>`).join('');if(data[current])select.value=current}el('savePreset').onclick=()=>{const name=el('presetName').value.trim(),list=parse();if(!name||!list.length)return alert('请填写方案名称并输入关键词。');const data=presets();data[name]=list;localStorage.setItem('xKeywordPresets',JSON.stringify(data));renderPresets();el('presetList').value=name};el('loadPreset').onclick=()=>{const data=presets(),name=el('presetList').value;if(!name||!data[name])return alert('请选择关键词方案。');keywords.value=data[name].join('\n');el('presetName').value=name;update()};renderPresets();
async function api(path,options={}){const node=el('accountNode')?.value||'a',headers={'Content-Type':'application/json','X-Account-Node':node,...(options.headers||{})};const r=await fetch(path,{...options,headers}),d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}start.onclick=async()=>{const list=parse(),startDate=el('startDate').value,endDate=el('endDate').value;if(!list.length)return alert('请先输入关键词。');if(startDate&&endDate&&startDate>endDate)return alert('开始日期不能晚于结束日期。');try{await api('/api/start',{method:'POST',body:JSON.stringify({account_node:el('accountNode').value,keywords:list,mode:mode.value,limit:Number(limit.value),max_scrolls:Number(scrolls.value),author_delay_min:Number(el('authorMin').value),author_delay_max:Number(el('authorMax').value),author_strategy:el('authorStrategy').value,start_date:startDate,end_date:endDate,timezone_offset:Number(el('timezoneOffset').value),search_type:el('searchType').value,slice_days:Number(el('sliceDays').value),no_growth_stop:Number(el('noGrowth').value),pace:el('pace').value,scroll_delay_min:Number(el('scrollDelayMin').value),scroll_delay_max:Number(el('scrollDelayMax').value),delay_min:Number(el('sliceDelayMin').value),delay_max:Number(el('sliceDelayMax').value),break_every:Number(el('breakEvery').value),break_min:Number(el('breakMin').value),break_max:Number(el('breakMax').value),retry_limit:Number(el('retryLimit').value),backoff_min:Number(el('backoffMin').value),backoff_max:Number(el('backoffMax').value)})});refresh();loadHistory()}catch(e){alert(e.message)}};stop.onclick=async()=>{if(confirm('停止所选账号节点并保留断点吗？'))try{await api('/api/stop',{method:'POST',body:JSON.stringify({account_node:el('accountNode').value})})}catch(e){alert(e.message)}};resume.onclick=async()=>{try{await api('/api/resume',{method:'POST',body:JSON.stringify({account_node:el('accountNode').value})});refresh()}catch(e){alert(e.message)}};
function esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}let historyCache=[],currentRunId='';async function loadHistory(){try{const h=await api('/api/history');historyCache=h.runs;el('historySummary').textContent=`共 ${h.run_count} 个历史任务，跨批次累计去重帖子 ${h.cumulative_posts} 条。`;el('batchPicker').innerHTML=h.runs.map(r=>`<option value="${esc(r.run_id)}">${esc(r.run_id)} · ${esc(r.keywords.join('、')||'无关键词')} · ${r.post_count} 条 · ${r.status==='completed'?'已完成':'未完成'}</option>`).join('')||'<option value="">暂无批次</option>';el('historyBody').innerHTML=h.runs.length?h.runs.map((r,i)=>`<tr><td>${esc(r.created_at)}</td><td><span class="tag ${r.status==='completed'?'ok':''}">${r.status==='completed'?'已完成':'未完成'}</span>${r.data_quality_warning?'<br><span class="tag">数据偏差警告</span>':''}</td><td title="${esc(r.keywords.join(' | '))}">${esc(r.keywords.slice(0,4).join('、'))}${r.keywords.length>4?'…':''}</td><td>${esc(r.start_date||'不限')} — ${esc(r.end_date||'不限')}<br><span class="sub">${r.search_type==='live'?'最新':'热门'} · ${r.slice_days?`每 ${r.slice_days} 天一片`:'不切片'} · 日期时区 UTC${Number(r.timezone_offset)>=0?'+':''}${r.timezone_offset} · ${r.pace==='safe'?'稳妥':r.pace==='balanced'?'均衡':'自定义'} · ${r.author_strategy==='per_slice'?'逐片查作者':'最后查作者'}</span></td><td>${r.post_count}</td><td>${r.resolved_authors} / ${r.unique_authors} 位已查询<br><span class="sub">${r.known_locations} 位有所在地</span></td><td><div class="buttons"><button class="primary mini" onclick="viewRun('${esc(r.run_id)}')">查看详情</button><button class="secondary mini" onclick="reuseRun(${i})">复用条件</button>${r.result_url?`<a class="result secondary mini" target="_blank" href="${r.result_url}">完整结果 ↗</a>`:''}${r.can_resume?`<button class="success mini" onclick="resumeRun('${esc(r.run_id)}')">续跑</button>`:''}</div></td></tr>`).join(''):'<tr><td colspan="7">还没有历史任务。</td></tr>'}catch(e){el('historySummary').textContent='历史记录读取失败：'+e.message}}window.reuseRun=i=>{const r=historyCache[i];keywords.value=r.keywords.join('\n');el('startDate').value=r.start_date||'';el('endDate').value=r.end_date||'';el('timezoneOffset').value=String(r.timezone_offset??8);el('searchType').value=r.search_type||'live';el('sliceDays').value=String(r.slice_days||0);el('noGrowth').value=r.no_growth_stop||5;el('authorStrategy').value=r.author_strategy||'per_slice';el('pace').value=r.pace||'balanced';mode.value='custom';limit.value=r.limit??20;scrolls.value=r.max_scrolls||12;applyMode();applyPace();update();window.scrollTo({top:0,behavior:'smooth'})};window.resumeRun=async runId=>{try{await api('/api/resume',{method:'POST',body:JSON.stringify({run_id:runId})});refresh();viewRun(runId)}catch(e){alert(e.message)}};el('refreshHistory').onclick=loadHistory;el('openBatch').onclick=()=>{const id=el('batchPicker').value;if(id)viewRun(id)};
function phaseText(phase){return phase==='completed'?'任务已完成':phase==='enriching_authors'?'帖子搜索完成，正在逐个查询作者所在地':phase==='author_lookup_paused'?'帖子搜索已完成，作者所在地查询目前暂停':phase==='search_paused'?'帖子搜索尚未完成，断点已保留，可继续任务':'正在按关键词和日期搜索帖子'}function table(headers,rows){return `<div class="detail-table"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.join(''):`<tr><td colspan="${headers.length}">暂无数据</td></tr>`}</tbody></table></div>`}function authorLocation(a){return a.account_based_in||((a.status==='unknown')?'X 未公开':(a.status==='failed')?'查询失败':'待查询')}window.viewRun=async runId=>{try{const d=await api('/api/run?run_id='+encodeURIComponent(runId));el('historySection').classList.add('hidden');el('runDetail').classList.remove('hidden');el('detailTitle').textContent=`任务 ${d.run_id}`;el('detailMeta').textContent=`关键词：${d.keywords.join('、')||'—'}　日期：${d.config.start_date||'不限'} 至 ${d.config.end_date||'不限'}`;el('detailPhase').textContent=phaseText(d.phase);const pct=d.expected_slices?Math.min(100,Math.round(d.completed_slices*100/d.expected_slices)):0;el('detailProgress').style.width=pct+'%';el('detailKpis').innerHTML=[[d.posts_count,'帖子'],[`${d.completed_slices}/${d.expected_slices}`,'搜索片'],[d.unique_authors,'去重作者'],[d.resolved_authors,'已查作者'],[d.known_locations,'已知所在地']].map(([v,l])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join('');el('detailActions').innerHTML=`${d.result_url?`<a class="result primary" target="_blank" href="${d.result_url}">打开完整可视化 ↗</a>`:''}${d.zip_url?`<a class="result secondary" href="${d.zip_url}">下载 ZIP</a>`:''}${d.can_resume?`<button class="success" onclick="resumeRun('${esc(d.run_id)}')">继续任务</button>`:''}`;el('postsPane').innerHTML=table(['关键词','正文','时间','作者','所在地'],d.posts_preview.map(p=>`<tr><td>${esc(p.keyword)}</td><td class="post-cell">${esc(p.post_text)}</td><td>${esc(p.posted_at)}</td><td>@${esc(p.author_username)}</td><td>${esc(p.account_based_in||'待查询')}</td></tr>`));el('authorsPane').innerHTML=table(['作者','用户ID','Account based in','状态'],d.authors.map(a=>`<tr><td>@${esc(a.username)}</td><td>${esc(a.user_id)}</td><td>${esc(authorLocation(a))}</td><td>${esc(a.status)}</td></tr>`));el('coveragePane').innerHTML=table(['关键词','日期片','帖子','滚动','停止原因','覆盖状态'],d.coverage.map(c=>`<tr><td>${esc(c.keyword)}</td><td>${esc(c.slice_start)} — ${esc(c.slice_end)}</td><td>${c.count}</td><td>${c.scrolls_used??''}</td><td>${esc(c.stop_reason)}</td><td>${esc(c.coverage_status)}</td></tr>`));el('detailLog').textContent=d.log_tail||'暂无日志';window.scrollTo({top:el('runDetail').offsetTop-20,behavior:'smooth'})}catch(e){alert('任务详情读取失败：'+e.message)}};el('backHistory').onclick=()=>{el('runDetail').classList.add('hidden');el('historySection').classList.remove('hidden');loadHistory();window.scrollTo({top:el('historySection').offsetTop-20,behavior:'smooth'})};for(const button of document.querySelectorAll('.tabs button'))button.onclick=()=>{for(const item of document.querySelectorAll('.tabs button'))item.classList.toggle('active',item===button);for(const pane of document.querySelectorAll('.tab-pane'))pane.classList.toggle('active',pane.id===button.dataset.tab)};
function renderNodes(nodes){const rows=Object.entries(nodes||{}),running=rows.filter(([,n])=>n.status==='running').length,resumable=rows.filter(([,n])=>n.can_resume).length;el('nodeTotal').textContent=rows.length;el('nodeRunning').textContent=running;el('nodeResumable').textContent=resumable;el('stopAll').disabled=!running;el('resumeAll').disabled=!resumable;el('nodeCards').innerHTML=rows.map(([key,n])=>{const active=el('accountNode').value===key,last=(n.logs||[]).slice(-1)[0]||'暂无运行日志',color=n.status==='running'?'#c86445':n.status==='failed'?'#a8483b':n.status==='success'?'#287a55':'#8a8178';return `<button type="button" aria-pressed="${active}" onclick="selectNode('${esc(key)}')" style="text-align:left;background:${active?'#fff8f1':'#fffdf9'};color:#312d29;border:1px solid ${active?'#c86445':'#ded8ce'};box-shadow:${active?'0 10px 24px rgba(160,76,48,.12)':'none'};padding:16px;border-radius:15px"><span style="display:flex;justify-content:space-between;gap:8px"><strong>${esc(n.node_label)}</strong><span style="color:${color}">● ${esc(n.label)}</span></span><small style="display:block;color:#746e66;margin-top:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">节点 ${esc(key.toUpperCase())} · ${esc(last)}</small></button>`}).join('')||'<p class="sub">暂无启用账号，请点击“添加/管理账号”。</p>'}window.selectNode=key=>{el('accountNode').value=key;currentRunId='';refresh()};
let accountsCache=[];function renderAccounts(accounts){accountsCache=accounts||[];const select=el('accountNode'),current=select.value,active=accountsCache.filter(a=>a.active);select.innerHTML=active.map(a=>`<option value="${esc(a.id)}">${esc(a.label)}（节点 ${esc(a.id.toUpperCase())}）</option>`).join('');if(active.some(a=>a.id===current))select.value=current;else if(active.length)select.value=active[0].id;select.disabled=!active.length;start.disabled=!active.length;el('accountList').innerHTML=accountsCache.map(a=>`<div class="account-row"><div><strong>${esc(a.label)}</strong><div class="sub">节点 ${esc(a.id.toUpperCase())} · ${a.active?'已启用':'已停用'} · ${a.status==='running'?'正在运行':'当前'+esc(a.state_label||'空闲')}</div></div><button class="secondary mini" onclick="renameAccount('${esc(a.id)}')">重命名</button><button class="${a.active?'danger':'success'} mini" ${a.status==='running'?'disabled title="运行中不能停用"':''} onclick="toggleAccount('${esc(a.id)}',${!a.active})">${a.active?'停用':'重新启用'}</button></div>`).join('')||'<p class="sub">暂无账号节点。</p>'}
async function loadAccounts(){try{const data=await api('/api/accounts');renderAccounts(data.accounts)}catch(e){alert('账号列表读取失败：'+e.message)}}window.renameAccount=async id=>{const item=accountsCache.find(a=>a.id===id),label=prompt('输入新的账号名称：',item?.label||'');if(label===null)return;try{const data=await api('/api/accounts/update',{method:'POST',body:JSON.stringify({id,label})});renderAccounts(data.accounts);refresh()}catch(e){alert(e.message)}};window.toggleAccount=async(id,active)=>{if(!active&&!confirm('停用后不会删除登录目录和历史任务，确定继续吗？'))return;try{const data=await api('/api/accounts/update',{method:'POST',body:JSON.stringify({id,active})});renderAccounts(data.accounts);refresh()}catch(e){alert(e.message)}};el('addAccount').onclick=async()=>{const label=el('newAccountName').value.trim();if(!label)return alert('请填写账号名称。');try{const data=await api('/api/accounts/create',{method:'POST',body:JSON.stringify({label})});renderAccounts(data.accounts);el('accountNode').value=data.created_id;el('newAccountName').value='';refresh()}catch(e){alert(e.message)}};el('showAccountManager').onclick=()=>{el('accountManager').classList.remove('hidden');el('showAccountManager').setAttribute('aria-expanded','true');el('newAccountName').focus()};el('closeAccountManager').onclick=()=>{el('accountManager').classList.add('hidden');el('showAccountManager').setAttribute('aria-expanded','false')};el('stopAll').onclick=async()=>{if(!confirm('确定暂停所有正在运行的账号，并为每个任务保留断点吗？'))return;el('stopAll').disabled=true;try{const data=await api('/api/stop-all',{method:'POST',body:'{}'}),failed=Object.keys(data.errors||{}).length;alert(`已向 ${data.stopped.length} 个账号发送暂停指令${failed?`，${failed} 个失败`:''}。`);setTimeout(refresh,800)}catch(e){alert(e.message)}};el('resumeAll').onclick=async()=>{if(!confirm('确定同时继续所有存在断点的启用账号吗？'))return;el('resumeAll').disabled=true;try{const data=await api('/api/resume-all',{method:'POST',body:'{}'}),failed=Object.keys(data.errors||{}).length;alert(`已继续 ${data.resumed.length} 个账号${failed?`，${failed} 个失败`:''}。`);refresh()}catch(e){alert(e.message)}};
async function refresh(){try{if(!el('accountNode').value)return;const s=await api('/api/status');renderNodes(s.nodes);el('status').textContent=s.node_label+' · '+s.label;el('dot').className='dot '+s.status;start.disabled=s.status==='running';stop.disabled=s.status!=='running';resume.disabled=s.status==='running'||!s.can_resume;log.textContent=s.logs.length?s.logs.join('\n'):'等待任务……';log.scrollTop=log.scrollHeight;el('output').textContent=s.output_dir?'输出目录：'+s.output_dir:'尚未开始。';currentRunId=s.output_dir?s.output_dir.replaceAll('\\','/').split('/').filter(Boolean).pop():'';if(currentRunId)el('currentDetail').classList.remove('hidden');else el('currentDetail').classList.add('hidden');if(s.result_url){result.href=s.result_url;result.classList.remove('hidden')}else result.classList.add('hidden')}catch(e){el('status').textContent='页面服务连接失败'}}el('currentDetail').onclick=()=>currentRunId&&viewRun(currentRunId);const reuseRunBase=window.reuseRun;window.reuseRun=i=>{const wanted=historyCache[i].account_node||'a';if(accountsCache.some(a=>a.id===wanted&&a.active))el('accountNode').value=wanted;reuseRunBase(i);refresh()};el('accountNode').onchange=()=>{currentRunId='';refresh()};el('refreshNodes').onclick=()=>{loadAccounts();refresh()};applyMode();applyPace();loadHistory();loadAccounts().then(refresh);setInterval(refresh,1000);setInterval(loadHistory,15000);</script></body></html>"""


def read_json(path: Path, default: object) -> object:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return default


def normalize_keywords(value: object) -> list[str]:
    if not isinstance(value, list): raise ValueError("关键词列表格式无效")
    output=[]; seen=set()
    for raw in value:
        term=str(raw).strip()
        if not term: continue
        if len(term)>200: raise ValueError(f"关键词过长：{term[:40]}…")
        if term.casefold() not in seen: seen.add(term.casefold()); output.append(term)
    if not output: raise ValueError("没有读取到关键词")
    if len(output)>MAX_KEYWORDS: raise ValueError(f"单个任务最多 {MAX_KEYWORDS} 个关键词")
    return output


def resolve_result(relative: str) -> Path:
    target=(OUTPUT_ROOT/relative).resolve()
    if not target.is_relative_to(OUTPUT_ROOT.resolve()): raise ValueError("非法结果路径")
    return target


class JobManager:
    def __init__(self,node_id:str)->None:
        self.node_id=normalize_node(node_id);self.node=ACCOUNT_NODES[self.node_id]
        self.lock=threading.Lock(); self.process:subprocess.Popen[str]|None=None; self.status="idle"; self.label="空闲"; self.logs:list[str]=[]; self.output_dir=self._latest_incomplete(); self.result_url=self._result_url(self.output_dir)
        if self.output_dir: self.label="空闲（可继续最近任务）"
    @staticmethod
    def _result_url(directory:Path|None)->str:
        return "/results/"+urllib.parse.quote(directory.relative_to(OUTPUT_ROOT).as_posix())+"/index.html" if directory and (directory/"index.html").exists() else ""
    @staticmethod
    def _incomplete(directory:Path)->bool:
        raw=read_json(directory/"raw.json",{});config=read_json(directory/"run_config.json",{});return isinstance(raw,dict) and bool(raw) and not bool(raw.get("finalized",False)) and not (isinstance(config,dict) and bool(config.get("data_quality_warning")))
    def _latest_incomplete(self)->Path|None:
        if not OUTPUT_ROOT.exists(): return None
        return next((p for p in sorted(OUTPUT_ROOT.iterdir(),reverse=True) if p.is_dir() and self._incomplete(p) and normalize_node(read_json(p/"run_config.json",{}).get("account_node","a"))==self.node_id),None)
    def snapshot(self)->dict[str,Any]:
        with self.lock: return {"node":self.node_id,"node_label":self.node["label"],"status":self.status,"label":self.label,"logs":self.logs[-1600:],"output_dir":str(self.output_dir or ""),"result_url":self.result_url,"can_resume":bool(self.output_dir and self._incomplete(self.output_dir) and self.status!="running")}
    def _command(self,directory:Path,config:dict[str,Any],resume:bool)->list[str]:
        cmd=[str(PYTHON),str(COLLECTOR),"--keywords-file",str(directory/"input_keywords.txt"),"--output",str(directory),"--session-dir",str(self.node["session_dir"]),"--limit",str(config.get("limit",20)),"--max-scrolls",str(config.get("max_scrolls",12)),"--author-delay-min",str(config.get("author_delay_min",8)),"--author-delay-max",str(config.get("author_delay_max",15)),"--direct-author-delay-min",str(config.get("author_delay_min",8)),"--direct-author-delay-max",str(config.get("author_delay_max",15)),"--author-strategy",str(config.get("author_strategy","per_slice")),"--search-type",str(config.get("search_type","live")),"--slice-days",str(config.get("slice_days",0)),"--no-growth-stop",str(config.get("no_growth_stop",5)),"--timezone-offset",str(config.get("timezone_offset",8)),"--delay-min",str(config.get("delay_min",8)),"--delay-max",str(config.get("delay_max",15)),"--scroll-delay-min",str(config.get("scroll_delay_min",3)),"--scroll-delay-max",str(config.get("scroll_delay_max",6)),"--break-every",str(config.get("break_every",20)),"--break-min",str(config.get("break_min",120)),"--break-max",str(config.get("break_max",240)),"--retry-limit",str(config.get("retry_limit",3)),"--backoff-min",str(config.get("backoff_min",120)),"--backoff-max",str(config.get("backoff_max",300))]
        if config.get("start_date"):cmd.extend(["--start-date",str(config["start_date"])])
        if config.get("end_date"):cmd.extend(["--end-date",str(config["end_date"])])
        if resume: cmd.append("--resume")
        return cmd
    def _launch(self,directory:Path,config:dict[str,Any],resume:bool=False)->None:
        self.status="running"; self.label="正在继续采集" if resume else "正在采集"; self.output_dir=directory; self.result_url=""
        if not resume:self.logs=[]
        target=config.get("limit",20);target_label=f"每片 {target} 条" if target else "不限条数，滚到无新增"
        self.logs.extend([f"{'继续' if resume else '启动'}任务：{config['keyword_count']} 个关键词，{target_label}，日期切片 {config.get('slice_days',0) or '关闭'}",f"输出目录：{directory}"])
        flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0
        self.process=subprocess.Popen(self._command(directory,config,resume),cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",bufsize=1,creationflags=flags);threading.Thread(target=self._read_output,daemon=True).start()
    def start(self,keywords:list[str],limit:int,max_scrolls:int,author_min:float,author_max:float,start_date:str="",end_date:str="",timezone_offset:float=8,search_type:str="live",slice_days:int=0,no_growth_stop:int=5,author_strategy:str="per_slice",rhythm:dict[str,Any]|None=None)->None:
        with self.lock:
            if self.process and self.process.poll() is None: raise RuntimeError("已有采集任务正在运行")
            directory=OUTPUT_ROOT/(datetime.now().strftime("%Y%m%d_%H%M%S")+"_"+self.node_id);directory.mkdir(parents=True,exist_ok=False);(directory/"input_keywords.txt").write_text("\n".join(keywords)+"\n",encoding="utf-8")
            config={"account_node":self.node_id,"keyword_count":len(keywords),"limit":limit,"max_scrolls":max_scrolls,"author_delay_min":author_min,"author_delay_max":author_max,"author_strategy":author_strategy,"start_date":start_date,"end_date":end_date,"timezone_offset":timezone_offset,"search_type":search_type,"slice_days":slice_days,"no_growth_stop":no_growth_stop,**(rhythm or {})};(directory/"run_config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8");self._launch(directory,config)
    def resume(self,run_id:str="")->None:
        with self.lock:
            if self.process and self.process.poll() is None: raise RuntimeError("已有采集任务正在运行")
            if run_id:self.output_dir=run_directory(run_id)
            if not self.output_dir or not self._incomplete(self.output_dir): raise RuntimeError("没有可继续的任务")
            config=read_json(self.output_dir/"run_config.json",{});
            if not isinstance(config,dict): raise RuntimeError("任务配置损坏")
            if normalize_node(config.get("account_node","a"))!=self.node_id: raise RuntimeError("该任务属于另一个采集账号节点")
            self._launch(self.output_dir,config,True)
    def _read_output(self)->None:
        process=self.process
        if not process or not process.stdout:return
        log_path=(self.output_dir/"collector.log") if self.output_dir else None
        log_handle=log_path.open("a",encoding="utf-8") if log_path else None
        for line in process.stdout:
            if log_handle:log_handle.write(line);log_handle.flush()
            with self.lock:self.logs.append(line.rstrip());self.logs=self.logs[-1800:]
        if log_handle:log_handle.close()
        code=process.wait()
        with self.lock:
            if self.status=="stopped":self.label="已停止（断点已保留）"
            elif code==0:self.status,self.label="success","采集完成"
            else:self.status,self.label="failed",f"运行失败（退出码 {code}）"
            self.result_url=self._result_url(self.output_dir)
    def stop(self)->None:
        with self.lock:
            p=self.process
            if not p or p.poll() is not None:raise RuntimeError("当前没有运行中的任务")
            self.status,self.label="stopped","正在停止"
            p.send_signal(signal.CTRL_BREAK_EVENT) if os.name=="nt" else p.terminate()


MANAGERS={node_id:JobManager(node_id) for node_id in ACCOUNT_NODES}


def accounts_payload() -> list[dict[str, Any]]:
    rows = []
    for node_id, node in ACCOUNT_NODES.items():
        manager = MANAGERS.get(node_id)
        snapshot = manager.snapshot() if manager else {"status": "idle", "label": "空闲"}
        rows.append({
            "id": node_id,
            "label": str(node["label"]),
            "active": bool(node.get("active", True)),
            "status": snapshot["status"],
            "state_label": snapshot["label"],
        })
    return rows


def create_account_node(label: object) -> str:
    clean_label = str(label or "").strip()
    if not clean_label:
        raise ValueError("账号名称不能为空")
    if len(clean_label) > 80:
        raise ValueError("账号名称最多 80 个字符")
    with ACCOUNT_LOCK:
        if len(ACCOUNT_NODES) >= 200:
            raise ValueError("单台电脑最多配置 200 个账号节点")
        node_id = next_node_id()
        ACCOUNT_NODES[node_id] = {
            "label": clean_label,
            "active": True,
            "session_dir": session_directory(node_id),
        }
        MANAGERS[node_id] = JobManager(node_id)
        save_account_nodes()
    return node_id


def update_account_node(node_id: object, label: object = None, active: object = None) -> None:
    node_id = normalize_node(node_id)
    with ACCOUNT_LOCK:
        node = ACCOUNT_NODES[node_id]
        if label is not None:
            clean_label = str(label).strip()
            if not clean_label:
                raise ValueError("账号名称不能为空")
            if len(clean_label) > 80:
                raise ValueError("账号名称最多 80 个字符")
            node["label"] = clean_label
        if active is not None:
            enabled = bool(active)
            manager = MANAGERS[node_id]
            if not enabled and manager.status == "running":
                raise RuntimeError("账号正在运行，不能停用")
            if not enabled and sum(bool(item.get("active", True)) for item in ACCOUNT_NODES.values()) <= 1:
                raise ValueError("至少保留一个启用账号")
            node["active"] = enabled
        save_account_nodes()


def stop_all_jobs() -> dict[str, Any]:
    stopped: list[str] = []
    skipped: list[str] = []
    errors: dict[str, str] = {}
    for node_id, manager in list(MANAGERS.items()):
        if not ACCOUNT_NODES[node_id].get("active", True) or manager.status != "running":
            skipped.append(node_id)
            continue
        try:
            manager.stop()
            stopped.append(node_id)
        except RuntimeError as exc:
            errors[node_id] = str(exc)
    return {"stopped": stopped, "skipped": skipped, "errors": errors}


def resume_all_jobs() -> dict[str, Any]:
    resumed: list[str] = []
    skipped: list[str] = []
    errors: dict[str, str] = {}
    for node_id, manager in list(MANAGERS.items()):
        if not ACCOUNT_NODES[node_id].get("active", True) or manager.status == "running":
            skipped.append(node_id)
            continue
        if not manager.snapshot().get("can_resume"):
            skipped.append(node_id)
            continue
        try:
            manager.resume()
            resumed.append(node_id)
        except RuntimeError as exc:
            errors[node_id] = str(exc)
    return {"resumed": resumed, "skipped": skipped, "errors": errors}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,format:str,*args:object)->None:return
    def send_bytes(self,data:bytes,content_type:str,status:int=200)->None:self.send_response(status);self.send_header("Content-Type",content_type);self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(data)
    def send_json(self,payload:dict[str,Any],status:int=200)->None:self.send_bytes(json.dumps(payload,ensure_ascii=False).encode(),"application/json; charset=utf-8",status)
    def read_json(self)->dict[str,Any]:
        length=int(self.headers.get("Content-Length","0"));
        if length>1_000_000:raise ValueError("请求过大")
        return json.loads(self.rfile.read(length).decode()) if length else {}
    def do_GET(self)->None:
        parsed=urllib.parse.urlsplit(self.path)
        if parsed.path=="/":self.send_bytes(PAGE.encode(),"text/html; charset=utf-8");return
        if parsed.path=="/api/accounts":self.send_json({"accounts":accounts_payload()});return
        if parsed.path=="/api/status":
            params=urllib.parse.parse_qs(parsed.query);node=normalize_node(params.get("node",[self.headers.get("X-Account-Node","a")])[0],True);snapshot=MANAGERS[node].snapshot();snapshot["nodes"]={key:manager.snapshot() for key,manager in MANAGERS.items() if ACCOUNT_NODES[key].get("active",True)};self.send_json(snapshot);return
        if parsed.path=="/api/history":
            runs=history_rows();self.send_json({"runs":runs,"run_count":len(runs),"cumulative_posts":len(cumulative_posts())});return
        if parsed.path=="/api/run":
            try:
                params=urllib.parse.parse_qs(parsed.query);run_id=str(params.get("run_id",[""])[0]);self.send_json(run_details(run_id));return
            except ValueError as exc:self.send_json({"error":str(exc)},HTTPStatus.BAD_REQUEST);return
        if parsed.path=="/history/cumulative_posts.csv":
            data=rows_csv(cumulative_posts());self.send_bytes(data,"text/csv; charset=utf-8");return
        if parsed.path.startswith("/results/"):
            try:
                target=resolve_result(urllib.parse.unquote(parsed.path[len("/results/"):]))
                if not target.is_file():raise FileNotFoundError(target)
                mime=mimetypes.guess_type(target.name)[0] or "application/octet-stream";self.send_bytes(target.read_bytes(),mime+("; charset=utf-8" if mime.startswith("text/") else ""));return
            except (ValueError,FileNotFoundError):self.send_json({"error":"结果文件不存在"},HTTPStatus.NOT_FOUND);return
        self.send_json({"error":"页面不存在"},HTTPStatus.NOT_FOUND)
    def do_POST(self)->None:
        try:
            data=self.read_json()
            if self.path=="/api/accounts/create":
                node_id=create_account_node(data.get("label"));self.send_json({"ok":True,"created_id":node_id,"accounts":accounts_payload()});return
            if self.path=="/api/accounts/update":
                update_account_node(data.get("id"),data.get("label") if "label" in data else None,data.get("active") if "active" in data else None);self.send_json({"ok":True,"accounts":accounts_payload()});return
            if self.path=="/api/stop-all":
                self.send_json({"ok":True,**stop_all_jobs()});return
            if self.path=="/api/resume-all":
                self.send_json({"ok":True,**resume_all_jobs()});return
            if self.path=="/api/start":
                node=normalize_node(data.get("account_node","a"),True);manager=MANAGERS[node]
                keywords=normalize_keywords(data.get("keywords"));mode=str(data.get("mode","smoke"))
                if mode=="smoke":limit,max_scrolls=5,4
                elif mode=="formal":limit,max_scrolls=500,150
                elif mode=="exhaustive":limit,max_scrolls=0,1000
                else:limit,max_scrolls=int(data.get("limit",20)),int(data.get("max_scrolls",12))
                pace=str(data.get("pace","safe"))
                if pace=="safe":
                    amin,amax=8.0,15.0;rhythm={"pace":"safe","scroll_delay_min":4,"scroll_delay_max":8,"delay_min":15,"delay_max":30,"break_every":15,"break_min":180,"break_max":360,"retry_limit":3,"backoff_min":180,"backoff_max":420}
                elif pace=="balanced":
                    amin,amax=8.0,15.0;rhythm={"pace":"balanced","scroll_delay_min":3,"scroll_delay_max":6,"delay_min":8,"delay_max":15,"break_every":20,"break_min":120,"break_max":240,"retry_limit":3,"backoff_min":120,"backoff_max":300}
                elif pace=="custom":
                    amin,amax=float(data.get("author_delay_min",12)),float(data.get("author_delay_max",25));rhythm={"pace":"custom","scroll_delay_min":float(data.get("scroll_delay_min",4)),"scroll_delay_max":float(data.get("scroll_delay_max",8)),"delay_min":float(data.get("delay_min",15)),"delay_max":float(data.get("delay_max",30)),"break_every":int(data.get("break_every",15)),"break_min":float(data.get("break_min",180)),"break_max":float(data.get("break_max",360)),"retry_limit":int(data.get("retry_limit",3)),"backoff_min":float(data.get("backoff_min",180)),"backoff_max":float(data.get("backoff_max",420))}
                else:raise ValueError("节奏档位无效")
                start_date,end_date=str(data.get("start_date","")).strip(),str(data.get("end_date","")).strip();timezone_offset=float(data.get("timezone_offset",8));search_type=str(data.get("search_type","live"));slice_days=int(data.get("slice_days",0));no_growth_stop=int(data.get("no_growth_stop",5));author_strategy=str(data.get("author_strategy","per_slice"))
                if not 0<=limit<=100000 or not 1<=max_scrolls<=10000:raise ValueError("目标数或滚动次数无效")
                if not 0<=slice_days<=3660 or not 2<=no_growth_stop<=50:raise ValueError("切片天数或无新增停止次数无效")
                if not 2<=amin<=amax<=300:raise ValueError("作者查询间隔必须为 2—300 秒")
                if not -12<=timezone_offset<=14:raise ValueError("日期时区必须在 UTC-12 到 UTC+14 之间")
                if not 1<=rhythm["scroll_delay_min"]<=rhythm["scroll_delay_max"]<=300 or not 0<=rhythm["delay_min"]<=rhythm["delay_max"]<=3600:raise ValueError("请求等待区间无效")
                if not 0<=rhythm["break_every"]<=10000 or not 0<=rhythm["break_min"]<=rhythm["break_max"]<=86400:raise ValueError("定期休息参数无效")
                if not 0<=rhythm["retry_limit"]<=10 or not 10<=rhythm["backoff_min"]<=rhythm["backoff_max"]<=86400:raise ValueError("退避参数无效")
                parsed_start=date.fromisoformat(start_date) if start_date else None;parsed_end=date.fromisoformat(end_date) if end_date else None
                if parsed_start and parsed_end and parsed_start>parsed_end:raise ValueError("开始日期不能晚于结束日期")
                if search_type not in {"live","top"}:raise ValueError("搜索排序无效")
                if author_strategy not in {"per_slice","after_search"}:raise ValueError("作者查询策略无效")
                manager.start(keywords,limit,max_scrolls,amin,amax,start_date,end_date,timezone_offset,search_type,slice_days,no_growth_stop,author_strategy,rhythm);self.send_json({"ok":True,"node":node});return
            if self.path=="/api/resume":
                run_id=str(data.get("run_id","")).strip();node=normalize_node(data.get("account_node") or (read_json(run_directory(run_id)/"run_config.json",{}).get("account_node","a") if run_id else "a"),True);MANAGERS[node].resume(run_id);self.send_json({"ok":True,"node":node});return
            if self.path=="/api/stop":
                node=normalize_node(data.get("account_node","a"),True);MANAGERS[node].stop();self.send_json({"ok":True,"node":node});return
            self.send_json({"error":"接口不存在"},HTTPStatus.NOT_FOUND)
        except (ValueError,RuntimeError,json.JSONDecodeError) as exc:self.send_json({"error":str(exc)},HTTPStatus.BAD_REQUEST)


def main()->int:
    parser=argparse.ArgumentParser(description="X关键词帖子采集器本地界面");parser.add_argument("--host",default="127.0.0.1");parser.add_argument("--port",type=int,default=8766);parser.add_argument("--no-open",action="store_true");args=parser.parse_args()
    if not PYTHON.exists():raise SystemExit(f"未找到项目 Python：{PYTHON}")
    OUTPUT_ROOT.mkdir(parents=True,exist_ok=True);server=ThreadingHTTPServer((args.host,args.port),Handler);url=f"http://{args.host}:{args.port}/";print(f"X关键词帖子采集器已启动：{url}");print("保持此窗口打开；按 Ctrl+C 关闭。")
    if not args.no_open:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:print("\n页面服务已关闭。")
    finally:server.server_close()
    return 0


if __name__=="__main__":raise SystemExit(main())
