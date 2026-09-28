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

