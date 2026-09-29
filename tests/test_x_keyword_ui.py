from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))

import x_keyword_ui as ui  # noqa: E402


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
        (directory / "raw.json").write_text(json.dumps({"requested_keywords": [keyword], "generated_at": run_id, "finalized": True, "posts": [dict(base, keyword=keyword, author_username="a")], "authors": {"a": {"username": "a", "user_id": "1", "account_based_in": "Japan", "status": "success"}}, "search_runs": [{"keyword": keyword, "slice_start": "2025-03-01", "slice_end": "2025-03-01", "count": 1}]}), encoding="utf-8")
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


def test_account_management_controls_are_present() -> None:
    assert 'id="showAccountManager"' in ui.PAGE
    assert 'id="addAccount"' in ui.PAGE
    assert 'id="accountList"' in ui.PAGE
    assert 'id="stopAll"' in ui.PAGE
    assert 'id="resumeAll"' in ui.PAGE
    assert "radial-gradient(circle at 12% 0%" in ui.PAGE
    assert 'class="settings-grid"' in ui.PAGE
    assert "采集范围" in ui.PAGE
    assert "时间与切片" in ui.PAGE
    assert "作者所在地补全" in ui.PAGE
    assert "高级节奏参数" in ui.PAGE


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

