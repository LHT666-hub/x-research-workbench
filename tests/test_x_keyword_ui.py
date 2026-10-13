from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))

import x_keyword_ui as ui  # noqa: E402
import x_keyword_collector as collector  # noqa: E402


def test_history_and_cross_run_dedup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui, "OUTPUT_ROOT", tmp_path)
    base = {
        "post_id": "88", "post_url": "https://x.com/a/status/88", "post_text": "historical post",
        "posted_at": "2025-03-01T00:00:00Z", "author_username": "a", "author_display_name": "A",
        "author_user_id": "1", "account_based_in": "Japan", "matched_keywords": "AI",
        "keyword_count": 1, "source": "about_this_account", "account_lookup_status": "success", "collected_at": "now",
    }
    for run_id, keyword in (("20250101_010101", "AI"), ("20250202_020202", "ChatGPT")):
        directory = tmp_path / run_id
        directory.mkdir()
        (directory / "run_config.json").write_text(json.dumps({"keyword_count": 1, "limit": 0, "max_scrolls": 1000, "start_date": "2025-01-01", "end_date": "2025-03-31", "search_type": "live", "slice_days": 1, "no_growth_stop": 8, "pace": "safe"}), encoding="utf-8")
        (directory / "status.json").write_text(json.dumps({"post_count": 1, "resolved_authors": 1}), encoding="utf-8")
        (directory / "raw.json").write_text(json.dumps({"requested_keywords": [keyword], "generated_at": run_id, "finalized": True, "posts": [dict(base, keyword=keyword, author_username="a")], "authors": {"a": {"username": "a", "user_id": "1", "profile_location": "", "profile_location_status": "pending", "account_based_in": "Japan", "status": "success"}}, "search_runs": [{"keyword": keyword, "slice_start": "2025-03-01", "slice_end": "2025-03-01", "count": 1}]}), encoding="utf-8")
        (directory / "research_dataset.json").write_text(json.dumps([dict(base, matched_keywords=keyword)]), encoding="utf-8")
        (directory / "index.html").write_text("ok", encoding="utf-8")

    history = ui.history_rows()
    assert len(history) == 2
    assert history[0]["start_date"] == "2025-01-01"
    assert history[0]["limit"] == 0
    assert history[0]["slice_days"] == 1
    assert history[0]["pace"] == "safe"
    cumulative = ui.cumulative_posts()
    assert len(cumulative) == 1
    assert cumulative[0]["matched_keywords"] == "AI | ChatGPT"
    csv_bytes = ui.rows_csv(cumulative)
    assert "historical post" in csv_bytes.decode("utf-8-sig")
    detail = ui.run_details("20250202_020202")
    assert detail["phase"] == "completed"
    assert detail["posts_count"] == 1
    assert detail["resolved_authors"] == 1
    assert detail["known_locations"] == 1
    assert detail["profile_location_backlog_count"] == 1
    assert not detail["can_profile_backfill"]  # Unknown search end is not trustworthy completion.


def test_dynamic_account_management(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(ui, "ACCOUNT_CONFIG_PATH", tmp_path / "account-nodes.json")
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "主账号", "active": True, "session_dir": tmp_path / "session-a"},
    })
    monkeypatch.setattr(ui, "MANAGERS", {"a": ui.JobManager("a")})

    created = ui.create_account_node("Claude 英文样本")
    assert created == "b"
    assert ui.ACCOUNT_NODES[created]["session_dir"] != ui.ACCOUNT_NODES["a"]["session_dir"]
    assert created in ui.MANAGERS

    ui.update_account_node(created, label="Claude 第二账号", active=False)
    accounts = ui.accounts_payload()
    assert next(row for row in accounts if row["id"] == created)["label"] == "Claude 第二账号"
    assert not ui.ACCOUNT_NODES[created]["active"]

    reloaded = ui.load_account_nodes()
    assert reloaded[created]["label"] == "Claude 第二账号"
    assert not reloaded[created]["active"]


def test_account_names_are_unique_and_snapshot_does_not_reparse_raw(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui, "OUTPUT_ROOT", tmp_path / "outputs")
    monkeypatch.setattr(ui, "ACCOUNT_CONFIG_PATH", tmp_path / "account-nodes.json")
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "主账号", "active": True, "session_dir": tmp_path / "session-a"},
    })
    manager = ui.JobManager("a")
    monkeypatch.setattr(ui, "MANAGERS", {"a": manager})

    with pytest.raises(ValueError, match="同名"):
        ui.create_account_node(" 主账号 ")

    manager.output_dir = tmp_path / "missing-run"
    manager.can_resume = True
    monkeypatch.setattr(manager, "_incomplete", lambda _directory: (_ for _ in ()).throw(AssertionError("hot snapshot reparsed raw.json")))
    snapshot = manager.snapshot(include_logs=False)
    assert snapshot["can_resume"] is True
    assert snapshot["logs"] == []


def test_account_management_controls_are_present() -> None:
    assert 'id="showAccountManager"' in ui.PAGE
    assert 'id="addAccount"' in ui.PAGE
    assert 'id="accountList"' in ui.PAGE
    assert 'id="accountManagerStatus"' in ui.PAGE
    assert 'id="stopAll"' in ui.PAGE
    assert 'id="resumeAll"' in ui.PAGE
    assert "radial-gradient(circle at 12% 0%" in ui.PAGE
    assert 'class="settings-grid"' in ui.PAGE
    assert "采集范围" in ui.PAGE
    assert "时间与切片" in ui.PAGE
    assert "作者所在地补全" in ui.PAGE
    assert "高级节奏参数" in ui.PAGE
    assert 'id="searchEngine"' in ui.PAGE
    assert 'value="graphql" selected' in ui.PAGE
    assert 'value="deferred" selected' in ui.PAGE
    assert "/api/backfill" in ui.PAGE
    assert "/api/profile-backfill" in ui.PAGE
    assert "/api/repair" in ui.PAGE
    assert "window.repairRun" in ui.PAGE
    assert "window.profileBackfillRun" in ui.PAGE
    assert "window.saveAccountName" in ui.PAGE
    assert "请勿重复点击" in ui.PAGE
    assert 'id="historyQuery"' in ui.PAGE
    assert 'id="historyAccount"' in ui.PAGE
    assert 'id="historyState"' in ui.PAGE
    assert 'id="historySort"' in ui.PAGE


def test_workbench_page_javascript_loads_without_syntax_errors(tmp_path: Path) -> None:
    page_path = tmp_path / "workbench.html"
    page_path.write_text(ui.PAGE, encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(collector.CHROME), headless=True)
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(page_path.as_uri())
        assert page.evaluate("typeof phaseText") == "function"
        assert page.evaluate("typeof window.backfillRun") == "function"
        assert page.evaluate("typeof window.repairRun") == "function"
        assert page.evaluate("typeof window.profileBackfillRun") == "function"
        assert page.evaluate("typeof window.saveAccountName") == "function"
        assert page.evaluate("typeof applyHistoryFilters") == "function"
        assert page.locator("#searchEngine").input_value() == "graphql"
        assert page.locator("#authorStrategy").input_value() == "deferred"
        assert errors == []
        browser.close()


def test_account_add_and_inline_rename_from_browser(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    monkeypatch.setattr(ui, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(ui, "ACCOUNT_CONFIG_PATH", tmp_path / "account-nodes.json")
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "主账号", "active": True, "session_dir": tmp_path / "session-a"},
    })
    monkeypatch.setattr(ui, "MANAGERS", {"a": ui.JobManager("a")})
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(collector.CHROME), headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.get_by_role("button", name="＋ 添加/管理账号").click()
            page.get_by_placeholder("例如：Claude 英文样本").fill("研究账号")
            page.get_by_role("button", name="添加账号节点").click()
            page.get_by_text("账号已添加为节点 B").wait_for()
            row = page.locator(".account-row", has_text="研究账号")
            row.get_by_role("button", name="重命名").click()
            page.locator("#accountEdit_b").fill("研究账号 B")
            page.locator(".account-row.editing").get_by_role("button", name="保存").click()
            page.get_by_text("节点 B 已重命名为“研究账号 B”").wait_for()
            assert page.locator(".account-row", has_text="研究账号 B").count() == 1
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_switching_accounts_ignores_old_status_response(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    monkeypatch.setattr(ui, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "账号 A", "active": True, "session_dir": tmp_path / "session-a"},
        "b": {"label": "账号 B", "active": True, "session_dir": tmp_path / "session-b"},
    })
    monkeypatch.setattr(ui, "MANAGERS", {node: ui.JobManager(node) for node in ("a", "b")})

    class SlowAHandler(ui.Handler):
        def do_GET(self) -> None:
            if self.path == "/api/status?node=a":
                time.sleep(0.4)
            super().do_GET()

    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), SlowAHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(collector.CHROME), headless=True)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.locator("#accountNode option[value=b]").wait_for(state="attached")
            page.locator("#accountNode").select_option("b")
            page.locator("#status").get_by_text("账号 B · 空闲").wait_for()
            page.wait_for_timeout(550)
            assert page.locator("#status").inner_text() == "账号 B · 空闲"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_bulk_stop_and_resume(monkeypatch) -> None:
    class FakeManager:
        def __init__(self, status: str, can_resume: bool = False) -> None:
            self.status = status
            self.can_resume = can_resume
            self.stopped = False
            self.resumed = False

        def snapshot(self) -> dict[str, object]:
            return {"can_resume": self.can_resume}

        def stop(self) -> None:
            self.stopped = True
            self.status = "stopped"
            self.can_resume = True

        def resume(self) -> None:
            self.resumed = True
            self.status = "running"
            self.can_resume = False

    running = FakeManager("running")
    paused = FakeManager("stopped", can_resume=True)
    inactive = FakeManager("running")
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"active": True}, "b": {"active": True}, "c": {"active": False},
    })
    monkeypatch.setattr(ui, "MANAGERS", {"a": running, "b": paused, "c": inactive})

    stopped = ui.stop_all_jobs()
    assert stopped["stopped"] == ["a"]
    assert running.stopped
    assert not inactive.stopped

    resumed = ui.resume_all_jobs()
    assert resumed["resumed"] == ["a", "b"]
    assert running.resumed and paused.resumed
    assert not inactive.resumed


def test_history_exposes_abnormal_date_repair_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ui, "OUTPUT_ROOT", tmp_path)
    run_id = "20260929_010203"
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / "run_config.json").write_text(json.dumps({
        "account_node": "a", "start_date": "2026-01-01", "end_date": "2026-01-02",
        "slice_days": 1, "search_engine": "graphql", "author_strategy": "deferred",
    }), encoding="utf-8")
    (directory / "raw.json").write_text(json.dumps({
        "requested_keywords": ["GPT"], "finalized": False, "posts": [],
        "authors": {"alice": {"username": "alice", "status": "pending"}},
        "search_runs": [
            {"keyword": "GPT", "slice_start": "2026-01-01", "slice_end": "2026-01-01",
             "count": 0, "stop_reason": "no_network_response", "coverage_status": "needs_retry"},
            {"keyword": "GPT", "slice_start": "2026-01-02", "slice_end": "2026-01-02",
             "count": 12, "stop_reason": "cursor_exhausted", "coverage_status": "apparently_exhausted"},
        ],
    }), encoding="utf-8")

    row = ui.history_rows()[0]
    detail = ui.run_details(run_id)

    assert row["repair_queue_count"] == 2
    assert row["can_repair"]
    assert detail["completed_slices"] == 0
    assert detail["repair_queue_count"] == 2
    assert detail["repair_queue"][0]["slice_start"] == "2026-01-01"


def test_pagination_not_triggered_is_a_repairable_slice() -> None:
    run = {
        "keyword": "GPT", "slice_start": "2026-01-03", "slice_end": "2026-01-03",
        "count": 37, "stop_reason": "pagination_not_triggered", "coverage_status": "needs_retry",
    }
    assert ui._search_run_needs_retry(run)
    assert ui.repair_queue_rows({"search_runs": [run]})[0]["slice_start"] == "2026-01-03"


def test_profile_backfill_command_is_separate_from_account_backfill(tmp_path: Path, monkeypatch) -> None:
    session_dir = tmp_path / "session"
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    monkeypatch.setattr(ui, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "账号 A", "active": True, "session_dir": session_dir},
    })
    manager = ui.JobManager("a")
    directory = tmp_path / "run"
    directory.mkdir()
    command = manager._command(directory, {"author_strategy": "deferred"}, True, True)
    assert "--profile-location-backfill-only" in command
    assert command[command.index("--author-strategy") + 1] == "deferred"
    assert command[command.index("--pagination-trigger-attempts") + 1] == "3"
    assert command[command.index("--pagination-trigger-timeout-ms") + 1] == "15000"


def test_profile_backfill_preserves_finalized_search_and_builds_its_own_queue(tmp_path: Path, monkeypatch) -> None:
    output_root = tmp_path / "outputs"
    run_id = "20260930_010203_a"
    directory = output_root / run_id
    directory.mkdir(parents=True)
    monkeypatch.setattr(ui, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(ui, "ACCOUNT_NODES", {
        "a": {"label": "账号 A", "active": True, "session_dir": tmp_path / "session"},
    })
    (directory / "run_config.json").write_text(json.dumps({
        "account_node": "a", "keyword_count": 1, "author_strategy": "deferred",
    }), encoding="utf-8")
    (directory / "raw.json").write_text(json.dumps({
        "requested_keywords": ["GPT"], "finalized": True,
        "posts": [{"author_username": "Alice", "author_user_id": "7"}],
        "authors": {"alice": {
            "username": "Alice", "user_id": "7", "profile_location": "",
            "profile_location_status": "pending", "account_based_in": "United States",
            "status": "success",
        }},
        "search_runs": [],
    }), encoding="utf-8")
    manager = ui.JobManager("a")
    launched: dict[str, object] = {}

    def fake_launch(path: Path, config: dict[str, object], resume: bool = False, profile_location_backfill_only: bool = False) -> None:
        launched.update(path=path, config=config, resume=resume, profile_only=profile_location_backfill_only)

    monkeypatch.setattr(manager, "_launch", fake_launch)
    manager.profile_backfill(run_id)

    raw = json.loads((directory / "raw.json").read_text(encoding="utf-8"))
    queue = json.loads((directory / "profile_location_queue.json").read_text(encoding="utf-8"))
    assert raw["finalized"] is True
    assert raw["active_phase"] == "profile_location_enriching"
    assert queue[0]["username"] == "Alice"
    assert launched["resume"] is True
    assert launched["profile_only"] is True

