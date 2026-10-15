"""Inspect the authoritative continuation checkpoint without a browser/login."""
import argparse
import json
from pathlib import Path

from x_workbench.storage import load_continuation_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to continuation_raw.json")
    args = parser.parse_args()
    state = load_continuation_state(args.path)
    print(json.dumps({
        "actual_saved_posts": len(state["posts"]),
        "active_phase": state.get("active_phase"),
        "current_date": state.get("current_date"),
        "current_progress": state.get("current_progress"),
        "guard_hold": state.get("guard_hold"),
        "finalized": state.get("finalized"),
        "updated_at": state.get("updated_at"),
        "checkpoint_backend": state.get("checkpoint_backend", "json"),
        "platform_all_posts_proven": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
