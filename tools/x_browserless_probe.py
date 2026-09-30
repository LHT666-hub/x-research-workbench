"""Small, isolated Twikit search pilot; never writes into a production run.

This is not a replacement collector. It uses an explicitly separate account,
stops on authentication/rate-limit errors, and does not assert full coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COOKIES = ROOT / "tmp" / "browserless_probe" / "cookies.json"
DEFAULT_OUTPUT = ROOT / "outputs" / "browserless_probe"
SHANGHAI = timezone(timedelta(hours=8))
SEARCH_PATH = re.compile(r"/i/api/graphql/[A-Za-z0-9_-]+/SearchTimeline\Z")


def _system_proxy() -> str | None:
    """Use the current user's ordinary Windows HTTP proxy, if configured."""
    if sys.platform != "win32":
        return None
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
        try:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            value = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
        except FileNotFoundError:
            return None
    if not enabled or not value:
        return None
    if ";" in value or "=" in value:
        entries = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        value = entries.get("https") or entries.get("http") or ""
    if not value:
        return None
    proxy = value if "://" in value else "http://" + value
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        return None
    return proxy


async def _network_preflight(proxy: str | None) -> bool:
    """Check reachability before prompting for private account credentials."""
    import httpx

    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=10, follow_redirects=True) as client:
            response = await client.head("https://x.com/")
        return response.status_code < 500
    except httpx.RequestError:
        return False


def _twikit_client():
    try:
        from twikit import Client
    except ImportError as exc:
        raise RuntimeError("请先安装 requirements-browserless.txt（仅试验环境）") from exc
    return Client("en-US", proxy=_system_proxy())


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            # X's traditional created_at format, also used by some Twikit releases.
            try:
                moment = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
            except ValueError:
                return ""
    else:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _in_local_day(timestamp: str, target: date) -> bool:
    if not timestamp:
        return False
    moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return moment.astimezone(SHANGHAI).date() == target


def normalize_tweet(tweet: Any) -> dict[str, Any] | None:
    """Map public Twikit fields into the workbench's existing post schema."""
    user = getattr(tweet, "user", None)
    post_id = str(getattr(tweet, "id", "") or "")
    username = str(getattr(user, "screen_name", "") or "")
    if not post_id or not username:
        return None
    posted_at = _iso_timestamp(
        getattr(tweet, "created_at_datetime", None)
        or getattr(tweet, "created_at", None)
    )
    if not posted_at:
        return None
    return {
        "post_id": post_id,
        "post_url": f"https://x.com/{username}/status/{post_id}",
        "post_text": str(getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or ""),
        "posted_at": posted_at,
        "author_username": username,
        "author_user_id": str(getattr(user, "id", "") or ""),
        "profile_location": str(getattr(user, "location", "") or ""),
        "account_based_in": None,
        "source": "twikit_search_timeline_probe",
    }


def _error_kind(exc: Exception) -> str:
    """Classify without copying exception text, which may include request data."""
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status == 429 or "ratelimit" in name or "toomanyrequests" in name:
        return "rate_limited"
    if status in {401, 403} or "unauthorized" in name or "forbidden" in name:
        return "authentication_or_permission"
    if status == 404 or "notfound" in name:
        return "endpoint_or_transaction_unavailable"
    if "challenge" in name or "captcha" in name:
        return "manual_login_required"
    return "request_failed"


async def probe_pages(client: Any, query: str, day: date, max_pages: int = 3,
                      delay_seconds: float = 3.0, sleep=asyncio.sleep) -> dict[str, Any]:
    if not 1 <= max_pages <= 5:
        raise ValueError("试验最多允许 1–5 页")
    if delay_seconds < 2:
        raise ValueError("页间隔不能低于 2 秒")
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages: list[dict[str, int]] = []
    stop_reason = "probe_page_cap"
    result = None
    for index in range(max_pages):
        if index:
            await sleep(delay_seconds)
        try:
            result = await (client.search_tweet(query, "Latest", count=20) if index == 0 else result.next())
        except Exception as exc:
            stop_reason = _error_kind(exc)
            break
        if result is None:
            stop_reason = "no_next_page"
            break
        page_items = list(result)
        if not page_items:
            stop_reason = "empty_page"
            break
        added = 0
        for tweet in page_items:
            row = normalize_tweet(tweet)
            if row is None or not _in_local_day(row["posted_at"], day):
                continue
            if row["post_id"] in seen:
                continue
            seen.add(row["post_id"])
            posts.append(row)
            added += 1
        pages.append({"page": index + 1, "returned": len(page_items), "added_in_date": added})
    return {"query": query, "date": day.isoformat(), "pages": pages,
            "posts": posts, "stop_reason": stop_reason,
            "coverage_status": "pilot_only_not_complete"}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
        temp = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def _safe_cookie_path(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT) and not resolved.is_relative_to(ROOT / "tmp"):
        raise ValueError("会话文件必须放在仓库外，或被 Git 忽略的 tmp/ 下")
    return resolved


def observed_search_endpoint(coverage_csv: Path) -> str:
    """Use only an endpoint actually recorded by our browser collector."""
    with coverage_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    observed = [row for row in records if row.get("search_engine") == "graphql"
                and row.get("endpoint_path") and row.get("request_method") == "GET"]
    if not observed:
        raise ValueError("覆盖报告中没有实际观察到的 GET SearchTimeline 请求")
    path = observed[-1]["endpoint_path"]
    if not SEARCH_PATH.fullmatch(path):
        raise ValueError("覆盖报告中的搜索路径格式不符合预期；拒绝猜测接口")
    return "https://x.com" + path


def compare_with_raw(probe: dict[str, Any], raw_path: Path) -> dict[str, Any]:
    """Read-only ID overlap; different sessions/times make this non-exhaustive."""
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    day = date.fromisoformat(probe["date"])
    original = {
        str(post.get("post_id"))
        for post in raw.get("posts", [])
        if isinstance(post, dict)
        and _in_local_day(str(post.get("posted_at", "")), day)
        and str(post.get("post_id", ""))
    }
    sample = {post["post_id"] for post in probe["posts"]}
    overlap = sample & original
    return {"historical_day_count": len(original), "probe_count": len(sample),
            "overlap_count": len(overlap),
            "probe_overlap_fraction": round(len(overlap) / len(sample), 3) if sample else None,
            "caveat": "仅比较帖子 ID；账号、采集时点和搜索排序可能导致差异，不证明任一方全量。"}


async def _login(args: argparse.Namespace) -> int:
    cookies = _safe_cookie_path(args.cookies)
    if cookies.exists():
        print("该试验会话已存在；不会重复登录或覆盖它。", file=sys.stderr)
        return 2
    proxy = _system_proxy()
    if not await _network_preflight(proxy):
        print("登录前连通性检查失败：Python 尚不能访问 X。请检查系统代理或网络，再重试；尚未询问账号密码。", file=sys.stderr)
        return 1
    cookies.parent.mkdir(parents=True, exist_ok=True)
    username = args.username or input("第六个账号的 X 用户名：").strip().lstrip("@")
    if not username:
        print("未输入用户名；不会尝试登录。", file=sys.stderr)
        return 2
    print("注意：下面输入的邮箱、密码和可能出现的 2FA 验证码会显示在当前终端屏幕上；不要截图或共享屏幕。程序不会把密码写入日志或命令行。")
    email = input("第六个账号的邮箱（可留空）：").strip() or None
    password = input("第六个账号的 X 密码（输入时可见）：")
    if not password:
        print("未输入密码；不会尝试登录。", file=sys.stderr)
        return 2
    client = _twikit_client()
    try:
        await client.login(auth_info_1=username, auth_info_2=email,
                           password=password, cookies_file=str(cookies))
    except Exception as exc:
        print(f"正常登录未完成：{_error_kind(exc)}。请自行处理任何验证要求；程序不会绕过。", file=sys.stderr)
        return 1
    finally:
        password = ""
    if not cookies.exists():
        print("登录返回成功，但未生成会话文件；不会开始采集。", file=sys.stderr)
        return 1
    print(f"试验账号登录完成。后续 probe 复用本机会话，无需每次输入密码或 2FA：{cookies}")
    return 0


async def _probe(args: argparse.Namespace) -> int:
    cookies = _safe_cookie_path(args.cookies)
    if not cookies.is_file():
        print("没有第六个账号的试验会话；请先执行 login。不会读取 A–E 会话。", file=sys.stderr)
        return 2
    client = _twikit_client()
    client.load_cookies(str(cookies))
    if args.observed_coverage:
        # Twikit 2.3.3 ships a static query ID. Only replace it with a path
        # actually captured in this workbench; never invent or rotate IDs.
        from twikit.client.gql import Endpoint
        Endpoint.SEARCH_TIMELINE = observed_search_endpoint(args.observed_coverage)
    day = date.fromisoformat(args.date)
    query = f"{args.keyword} since:{day.isoformat()} until:{day.isoformat()}"
    started = time.monotonic()
    result = await probe_pages(client, query, day, args.max_pages, args.delay_seconds)
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    result["requested_pages"] = args.max_pages
    result["account_based_in_lookup_attempted"] = False
    result["search_endpoint_source"] = "observed_coverage" if args.observed_coverage else "twikit_bundled"
    result["note"] = "试验数据，绝不代表当日全量；没有写入 A–E 的 raw.json。"
    if args.compare_raw:
        result["comparison"] = compare_with_raw(result, args.compare_raw)
    filename = f"{day.isoformat()}_{int(time.time())}.json"
    target = args.output.resolve() / filename
    _atomic_json(target, result)
    print(json.dumps({"output": str(target), "posts": len(result["posts"]),
                      "pages": len(result["pages"]), "stop_reason": result["stop_reason"],
                      "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False))
    return 0 if result["pages"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第六账号专用的无浏览器小规模试验")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="仅用第六个账号正常登录一次")
    login.add_argument("--username", help="省略时在本机终端交互输入")
    login.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES)
    probe = commands.add_parser("probe", help="最多五页，结果独立保存")
    probe.add_argument("--keyword", required=True)
    probe.add_argument("--date", required=True)
    probe.add_argument("--max-pages", type=int, default=3)
    probe.add_argument("--delay-seconds", type=float, default=3.0)
    probe.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES)
    probe.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    probe.add_argument("--compare-raw", type=Path, help="只读对比一个现有批次的 raw.json")
    probe.add_argument("--observed-coverage", type=Path,
                       help="从现有 coverage_report.csv 读取真实观察到的搜索路径；不读取登录会话")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_login(args) if args.command == "login" else _probe(args))
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
