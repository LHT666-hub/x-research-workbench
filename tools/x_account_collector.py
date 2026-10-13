from __future__ import annotations

import argparse
import csv
import html
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_DIR = ROOT / "tmp" / "chrome-x-account-profile"
OUTPUT_ROOT = ROOT / "outputs" / "x_account_runs"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

FIELDS = [
    "requested_username", "username", "user_id", "display_name",
    "account_based_in", "profile_location", "source", "connected_via",
    "created_at", "created_country_accurate", "location_accurate",
    "username_change_count", "last_username_change_at", "verified_since",
    "verified", "is_blue_verified", "protected", "followers_count",
    "following_count", "posts_count", "media_count", "bio", "profile_url",
    "status", "error", "collected_at",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def msec_iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def normalize_username(value: str) -> str:
    value = value.strip()
    if not value or value.startswith("#"):
        return ""
    if "://" in value:
        parts = urlsplit(value)
        if parts.netloc.lower().removeprefix("www.") not in {"x.com", "twitter.com"}:
            raise ValueError(f"不是 X/Twitter 主页：{value}")
        value = unquote(parts.path.strip("/").split("/")[0])
    value = value.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        raise ValueError(f"用户名格式无效：{value}")
    return value


def read_accounts(path: Path) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        username = normalize_username(raw)
        if username and username.casefold() not in seen:
            seen.add(username.casefold())
            output.append(username)
    return output


def write_csv(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def save_outputs(output_dir: Path, payload: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = list(payload.get("records", []))
    (output_dir / "raw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(output_dir / "accounts.csv", records)
    summary = {
        "requested": len(payload.get("requested_accounts", [])),
        "completed": len(records),
        "success": sum(r.get("status") == "success" for r in records),
        "unknown": sum(not r.get("account_based_in") for r in records),
        "updated_at": now_iso(),
    }
    (output_dir / "status.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "index.html").write_text(html_report(records, summary), encoding="utf-8")


def html_report(records: list[dict[str, object]], summary: dict[str, object]) -> str:
    rows = []
    for item in records:
        cells = "".join(
            f"<td>{html.escape(str(item.get(field, '') or ''))}</td>"
            for field in FIELDS
        )
        rows.append(f"<tr>{cells}</tr>")
    headers = "".join(f"<th>{html.escape(field)}</th>" for field in FIELDS)
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>X账号采集结果</title>
<style>body{{margin:0;background:#f4f7fa;color:#182230;font-family:system-ui,'Microsoft YaHei',sans-serif}}main{{padding:24px;max-width:1600px;margin:auto}}h1{{margin:0 0 8px}}.sub{{color:#66788a}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin:18px 0}}.card{{background:white;border:1px solid #d9e2ec;border-radius:10px;padding:14px}}.card b{{display:block;font-size:24px;color:#1769aa}}.bar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}}input{{padding:10px 12px;border:1px solid #aebdca;border-radius:7px;min-width:300px}}a{{background:#1769aa;color:white;text-decoration:none;padding:10px 14px;border-radius:7px;font-weight:700}}.table{{overflow:auto;background:white;border:1px solid #d9e2ec;border-radius:10px;max-height:70vh}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}th,td{{border-bottom:1px solid #e7edf2;padding:9px 10px;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}}th{{position:sticky;top:0;background:#17324d;color:white;z-index:1}}tr:hover td{{background:#f0f7ff}}@media(max-width:700px){{.cards{{grid-template-columns:repeat(2,1fr)}}}}</style></head>
<body><main><h1>X / Twitter 账号所在地采集结果</h1><p class='sub'>Account based in 来自 About this account；空值统一保留为 Unknown。</p>
<div class='cards'><div class='card'><b>{summary['requested']}</b>输入账号</div><div class='card'><b>{summary['completed']}</b>已处理</div><div class='card'><b>{summary['success']}</b>成功</div><div class='card'><b>{summary['unknown']}</b>所在地未知</div></div>
<div class='bar'><input id='q' placeholder='筛选用户名、国家、位置、简介……'><a href='accounts.csv'>下载 CSV</a><a href='raw.json'>下载 JSON</a></div>
<div class='table'><table><thead><tr>{headers}</tr></thead><tbody id='body'>{''.join(rows)}</tbody></table></div></main>
<script>const q=document.getElementById('q');q.oninput=()=>{{const v=q.value.toLowerCase();for(const r of document.querySelectorAll('#body tr'))r.hidden=!r.textContent.toLowerCase().includes(v)}};</script></body></html>"""


def wait_for_login(context, page, timeout_seconds: int) -> None:
    names = {cookie["name"] for cookie in context.cookies()}
    if "auth_token" in names:
        return
    page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=90_000)
    print("[需要登录] 请在弹出的研究 Chrome 中正常登录 X。不要关闭窗口。", flush=True)
    for _ in range(timeout_seconds):
        names = {cookie["name"] for cookie in context.cookies()}
        if "auth_token" in names:
            print("[登录完成] 开始采集。", flush=True)
            return
        page.wait_for_timeout(1000)
    raise RuntimeError("等待登录超时。请重新启动任务后完成正常登录。")


def safe_json(response) -> dict[str, object] | None:
    try:
        value = response.json()
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def profile_result(payload: dict[str, object] | None) -> dict[str, object]:
    try:
        value = payload["data"]["user"]["result"]  # type: ignore[index]
        return value if isinstance(value, dict) else {}
    except (KeyError, TypeError):
        return {}


def about_result(payload: dict[str, object] | None) -> dict[str, object]:
    try:
        value = payload["data"]["user_result_by_screen_name"]["result"]  # type: ignore[index]
        return value if isinstance(value, dict) else {}
    except (KeyError, TypeError):
        return {}


def make_record(requested: str, profile: dict[str, object], about: dict[str, object]) -> dict[str, object]:
    core = about.get("core") if isinstance(about.get("core"), dict) else profile.get("core", {})
    core = core if isinstance(core, dict) else {}
    about_profile = about.get("about_profile", {})
    about_profile = about_profile if isinstance(about_profile, dict) else {}
    location = profile.get("location", {})
    location = location if isinstance(location, dict) else {}
    counts = profile.get("relationship_counts", {})
    counts = counts if isinstance(counts, dict) else {}
    tweet_counts = profile.get("tweet_counts", {})
    tweet_counts = tweet_counts if isinstance(tweet_counts, dict) else {}
    bio = profile.get("profile_bio", {})
    bio = bio if isinstance(bio, dict) else {}
    privacy = about.get("privacy", profile.get("privacy", {}))
    privacy = privacy if isinstance(privacy, dict) else {}
    verification = about.get("verification", profile.get("verification", {}))
    verification = verification if isinstance(verification, dict) else {}
    verification_info = about.get("verification_info", profile.get("verification_info", {}))
    verification_info = verification_info if isinstance(verification_info, dict) else {}
    reason = verification_info.get("reason", {})
    reason = reason if isinstance(reason, dict) else {}
    changes = about_profile.get("username_changes", {})
    changes = changes if isinstance(changes, dict) else {}
    country = str(about_profile.get("account_based_in", "") or "")
    return {
        "requested_username": requested,
        "username": core.get("screen_name", requested),
        "user_id": about.get("rest_id", profile.get("rest_id", "")),
        "display_name": core.get("name", ""),
        "account_based_in": country,
        "profile_location": location.get("location", "") or "",
        "source": "about_this_account",
        "connected_via": about_profile.get("source", ""),
        "created_at": core.get("created_at", ""),
        "created_country_accurate": about_profile.get("created_country_accurate", ""),
        "location_accurate": about_profile.get("location_accurate", ""),
        "username_change_count": changes.get("count", ""),
        "last_username_change_at": msec_iso(changes.get("last_changed_at_msec")),
        "verified_since": msec_iso(reason.get("verified_since_msec")),
        "verified": verification.get("verified", ""),
        "is_blue_verified": about.get("is_blue_verified", profile.get("is_blue_verified", "")),
        "protected": privacy.get("protected", ""),
        "followers_count": counts.get("followers", ""),
        "following_count": counts.get("following", ""),
        "posts_count": tweet_counts.get("tweets", ""),
        "media_count": tweet_counts.get("media_tweets", ""),
        "bio": bio.get("description", ""),
        "profile_url": f"https://x.com/{requested}",
        "status": "success" if country else "unknown",
        "error": "",
        "collected_at": now_iso(),
    }


def collect(args: argparse.Namespace, accounts: list[str], output_dir: Path) -> None:
    payload: dict[str, object]
    raw_path = output_dir / "raw.json"
    if args.resume and raw_path.exists():
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        if payload.get("requested_accounts") != accounts:
            raise RuntimeError("断点账号列表与当前输入不一致。")
    else:
        payload = {"requested_accounts": accounts, "records": [], "generated_at": now_iso()}
    records = list(payload.get("records", []))
    completed = {str(row.get("requested_username", "")).casefold() for row in records}
    payload["records"] = records
    save_outputs(output_dir, payload)

    if not CHROME.exists():
        raise RuntimeError(f"未找到 Chrome：{CHROME}")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(args.session_dir.resolve()), headless=False, executable_path=str(CHROME),
            locale="en-US", viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(context, page, args.login_timeout)

        pending = [name for name in accounts if name.casefold() not in completed]
        for position, username in enumerate(pending, 1):
            if position > 1:
                delay = random.uniform(args.delay_min, args.delay_max)
                print(f"[等待] {delay:.1f} 秒后处理下一个账号。", flush=True)
                time.sleep(delay)
            print(f"[{position}/{len(pending)}] @{username}", flush=True)
            profile_holder: dict[str, object] = {}

            def capture_profile(response) -> None:
                if "UserByScreenName" not in response.url or response.status != 200:
                    return
                data = safe_json(response)
                result = profile_result(data)
                core = result.get("core", {}) if isinstance(result, dict) else {}
                if isinstance(core, dict) and str(core.get("screen_name", "")).casefold() == username.casefold():
                    profile_holder.update(result)

            page.on("response", capture_profile)
            record: dict[str, object]
            try:
                page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=90_000)
                page.wait_for_timeout(args.render_wait_ms)
                body = page.locator("body").inner_text(timeout=15_000)
                if "This account doesn’t exist" in body or "This account doesn't exist" in body:
                    raise RuntimeError("账号不存在")
                actions = page.locator('[data-testid="userActions"]')
                if not actions.count():
                    raise RuntimeError("未找到账号菜单；请确认登录仍有效")
                actions.click(timeout=20_000)
                about_item = page.get_by_text(re.compile(r"^About this account$", re.I), exact=True).last
                try:
                    with page.expect_response(lambda r: "AboutAccountQuery" in r.url, timeout=60_000) as pending_about:
                        about_item.click(timeout=20_000)
                    about_response = pending_about.value
                except PlaywrightTimeoutError:
                    with page.expect_response(lambda r: "AboutAccountQuery" in r.url, timeout=60_000) as pending_about:
                        page.goto(f"https://x.com/{username}/about", wait_until="domcontentloaded", timeout=90_000)
                    about_response = pending_about.value
                if about_response.status in {401, 403}:
                    raise RuntimeError(f"AboutAccountQuery HTTP {about_response.status}；登录会话可能失效")
                if about_response.status == 429:
                    raise RuntimeError("AboutAccountQuery HTTP 429；已触发限流，请稍后续跑")
                about = about_result(safe_json(about_response))
                if not about:
                    raise RuntimeError("AboutAccountQuery 未返回 User 结果")
                record = make_record(username, profile_holder, about)
                print(
                    f"  完成：user_id={record['user_id']}，account_based_in={record['account_based_in'] or 'Unknown'}",
                    flush=True,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                record = {field: "" for field in FIELDS}
                record.update({
                    "requested_username": username,
                    "username": username,
                    "source": "about_this_account",
                    "profile_url": f"https://x.com/{username}",
                    "status": "failed",
                    "error": message,
                    "collected_at": now_iso(),
                })
                print(f"  失败：{message}", flush=True)
            finally:
                page.remove_listener("response", capture_profile)
            records.append(record)
            payload["records"] = records
            payload["updated_at"] = now_iso()
            save_outputs(output_dir, payload)
            fatal_markers = ("HTTP 401", "HTTP 403", "HTTP 429", "登录仍有效", "登录会话")
            if any(marker in str(record.get("error", "")) for marker in fatal_markers):
                raise RuntimeError(str(record["error"]))
        context.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量采集 X About this account 账号所在地字段。")
    parser.add_argument("--accounts-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR)
    parser.add_argument("--delay-min", type=float, default=8)
    parser.add_argument("--delay-max", type=float, default=15)
    parser.add_argument("--render-wait-ms", type=int, default=3500)
    parser.add_argument("--login-timeout", type=int, default=900)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 0 <= args.delay_min <= args.delay_max:
            raise ValueError("等待区间无效")
        accounts = read_accounts(args.accounts_file)
        if not accounts:
            raise ValueError("没有读取到有效的 X 用户名")
        print(f"账号数：{len(accounts)}", flush=True)
        print(f"输出目录：{args.output.resolve()}", flush=True)
        if args.dry_run:
            print("DRY RUN：输入校验通过，未访问 X。", flush=True)
            return 0
        collect(args, accounts, args.output.resolve())
        print("全部账号处理完成。", flush=True)
        return 0
    except KeyboardInterrupt:
        print("用户中止；已完成账号已保存，可断点续跑。", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
