"""Synthetic storage-only benchmark; never opens accounts or sends requests."""
import argparse
import json
from pathlib import Path
import platform
import tempfile
import time

from x_workbench.storage import SQLiteCheckpoint, write_json_atomic


def measure_backend(folder, backend, initial, pages):
    path = folder / "continuation_raw.json"
    payload = {"posts": [{"post_id": str(i), "post_text": "synthetic gpt benchmark 科研 " * 8}
                         for i in range(initial)], "last_cursor": "initial", "guard_hold": None}
    store = SQLiteCheckpoint(path) if backend == "sqlite" else None
    start = time.perf_counter()
    if store:
        store.commit(payload)
        store.export_snapshot(payload)
    else:
        write_json_atomic(path, payload)
    bootstrap = time.perf_counter() - start
    start = time.perf_counter()
    try:
        for page in range(pages):
            for _ in range(20):
                payload["posts"].append({"post_id": str(len(payload["posts"])), "post_text": "synthetic gpt benchmark 科研 " * 8})
            payload["last_cursor"] = "page-" + str(page + 1)
            if store:
                store.commit(payload)
                if (page + 1) % 20 == 0:
                    store.export_snapshot(payload)
            else:
                write_json_atomic(path, payload)
        page_seconds = time.perf_counter() - start
        start = time.perf_counter()
        if store:
            store.export_snapshot(payload)
        final_export = time.perf_counter() - start
        assert json.loads(path.read_text(encoding="utf-8")) == payload
        return {"bootstrap_seconds": round(bootstrap, 6), "page_commits_seconds": round(page_seconds, 6),
                "final_snapshot_seconds": round(final_export, 6), "saved_posts": len(payload["posts"]),
                "snapshot_bytes": path.stat().st_size}
    finally:
        if store:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial-posts", type=int, default=10000)
    parser.add_argument("--pages", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.initial_posts <= 100000 or not 1 <= args.pages <= 100:
        raise ValueError("Bounded synthetic benchmark only")
    result = {"kind": "synthetic_storage_not_collection_throughput", "python": platform.python_version(),
              "initial_posts": args.initial_posts, "pages": args.pages, "posts_per_page": 20,
              "real_accounts_used": False, "real_requests": 0, "runs": []}
    with tempfile.TemporaryDirectory(prefix="x-research-offline-storage-") as name:
        root = Path(name)
        for repetition in range(3):
            for backend in (("json", "sqlite") if repetition % 2 == 0 else ("sqlite", "json")):
                result["runs"].append({"backend": backend, "repetition": repetition + 1,
                    **measure_backend(root / f"{repetition}-{backend}", backend, args.initial_posts, args.pages)})
    result["caveat"] = "Warm local disk; both backends durable; SQLite includes a periodic full snapshot every 20 page commits. Not parsing, HTTP, evidence or whole-worker speed."
    if args.output:
        write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
