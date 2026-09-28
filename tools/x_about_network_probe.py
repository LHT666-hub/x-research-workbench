import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from x_account_collector import CHROME, about_result, wait_for_login
from x_keyword_collector import DEFAULT_SESSION_DIR, lookup_author


def json_path_for_value(value, target, prefix="$p"):
    if isinstance(value, dict):
        for key, child in value.items():
            found = json_path_for_value(child, target, f"{prefix}.{key}")
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = json_path_for_value(child, target, f"{prefix}[{index}]")
            if found:
                return found
    elif value == target:
        return prefix
    return ""


def replace_screen_name(url: str, username: str) -> tuple[str, str]:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    parameter_location = ""
    for key, values in query.items():
        rewritten = []
        for value in values:
            if key == "variables":
                variables = json.loads(value)
                for candidate in ("screen_name", "screenName", "username"):
                    if candidate in variables:
                        variables[candidate] = username
                        parameter_location = f"query.{key}.{candidate}"
                value = json.dumps(variables, separators=(",", ":"))
            rewritten.append(value)
        query[key] = rewritten
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)), parameter_location


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", default="leige88888")
    parser.add_argument("--second", default="wyrmstepper")
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    captured = {}
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(args.session_dir.resolve()), headless=False, executable_path=str(CHROME),
            locale="en-US", viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        wait_for_login(context, page, 300)

        def remember(response):
            if "AboutAccountQuery" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            captured.update({"response": response, "payload": payload})

        page.on("response", remember)
        try:
            first_record = lookup_author(page, args.first, 8000)
        except RuntimeError:
            page.wait_for_timeout(5000)
            first_record = lookup_author(page, args.first, 8000)
        if not captured:
            raise RuntimeError("未观察到 AboutAccountQuery 网络响应")

        response = captured["response"]
        request = response.request
        payload = captured["payload"]
        first_result = about_result(payload)
        direct_url, parameter_location = replace_screen_name(response.url, args.second)

        # Reuse only the headers from the request X itself generated. They remain in memory
        # and are never written to the report.
        observed_headers = request.all_headers()
        forwarded_names = {
            "authorization", "content-type", "x-csrf-token", "x-twitter-active-user",
            "x-twitter-auth-type", "x-twitter-client-language", "x-client-transaction-id",
        }
        replay_headers = {
            key: value for key, value in observed_headers.items()
            if key.lower() in forwarded_names
        }
        replay_data = page.evaluate(
            """async ({url, method, headers, body}) => {
                const response = await fetch(url, {
                    method, headers, body: method === 'GET' ? undefined : body,
                    credentials: 'include'
                });
                let payload = {};
                try { payload = await response.json(); } catch (_) {}
                return {status: response.status, ok: response.ok, payload};
            }""",
            {"url": direct_url, "method": request.method, "headers": replay_headers, "body": request.post_data},
        )
        replay_payload = replay_data.get("payload", {}) if replay_data.get("ok") else {}
        replay_result = about_result(replay_payload)
        replay_about = replay_result.get("about_profile", {}) if isinstance(replay_result, dict) else {}
        replay_about = replay_about if isinstance(replay_about, dict) else {}
        replay_country = str(replay_about.get("account_based_in", "") or "")

        first_country = str(first_record.get("account_based_in", "") or "")
        report = {
            "observed": True,
            "operation_name": "AboutAccountQuery",
            "method": request.method,
            "endpoint": urlsplit(response.url).scheme + "://" + urlsplit(response.url).netloc + urlsplit(response.url).path,
            "request_parameter_location": parameter_location,
            "response_status": response.status,
            "first_username": args.first,
            "first_user_id": first_result.get("rest_id", "") if isinstance(first_result, dict) else "",
            "first_account_based_in": first_country,
            "response_json_path": json_path_for_value(payload, first_country) if first_country else "$p.data.user_result_by_screen_name.result.about_profile.account_based_in",
            "direct_replay": {
                "attempted": True,
                "username": args.second,
                "status": replay_data.get("status", 0),
                "success": bool(replay_data.get("ok") and replay_result),
                "user_id": replay_result.get("rest_id", "") if isinstance(replay_result, dict) else "",
                "account_based_in": replay_country,
            },
            "authentication": {
                "logged_in_session_required_for_test": True,
                "credentials_recorded": False,
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        context.close()


if __name__ == "__main__":
    try:
        main()
    except PlaywrightTimeoutError as exc:
        raise SystemExit(f"Playwright timeout: {exc}")
