"""Synthetic installation tests. No X requests or browser launches."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from x_workbench import runtime


def test_explicit_chrome_override_is_authoritative(tmp_path):
    missing = tmp_path / "missing.exe"
    assert runtime.discover_chrome({"X_WORKBENCH_CHROME": str(missing)}, which=lambda _: None) == missing


def test_chrome_override_accepts_quoted_path(tmp_path):
    path = tmp_path / "Chrome Browser/chrome.exe"
    assert runtime.discover_chrome({"X_WORKBENCH_CHROME": f'"{path}"'}, which=lambda _: None) == path


@pytest.mark.parametrize("variable", ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"])
def test_chrome_discovers_standard_and_user_installs(tmp_path, variable):
    binary = tmp_path / "Google/Chrome/Application/chrome.exe"
    binary.parent.mkdir(parents=True)
    binary.touch()
    env = {"PROGRAMFILES": str(tmp_path / "not-system"),
           "PROGRAMFILES(X86)": str(tmp_path / "not-x86"), variable: str(tmp_path)}
    assert runtime.discover_chrome(env, which=lambda _: None) == binary


def test_chrome_path_lookup(tmp_path):
    binary = tmp_path / "chrome"
    binary.touch()
    env = {"PROGRAMFILES": str(tmp_path / "missing"), "PROGRAMFILES(X86)": str(tmp_path / "missing-x86")}
    assert runtime.discover_chrome(env, which=lambda name: str(binary) if name == "chrome" else None) == binary


def test_diagnostics_do_not_access_accounts_or_network(tmp_path, monkeypatch):
    for name in ("tools/x_keyword_ui.py", "tools/x_keyword_collector.py", "requirements.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    binary = tmp_path / "chrome.exe"
    binary.touch()
    monkeypatch.setattr(runtime, "discover_chrome", lambda: binary)
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: object())
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    report = runtime.diagnose_runtime(tmp_path)
    assert report["ok"]
    assert report["network_requests"] == 0
    assert report["login_checked"] is False
    assert before == {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}


def test_diagnostics_missing_dependencies_and_project_files(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "discover_chrome", lambda: tmp_path / "missing.exe")
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: None)
    report = runtime.diagnose_runtime(tmp_path)
    assert not report["ok"]
    failed = {c["name"] for c in report["checks"] if c["level"] == "error"}
    assert {"chrome", "playwright", "project_files"} <= failed


def test_diagnostics_json_and_exit_codes(capsys):
    assert runtime.print_diagnostics({"ok": False, "checks": []}, as_json=True) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert runtime.print_diagnostics({"ok": True, "checks": []}, as_json=True) == 0


def test_ui_doctor_exits_before_server_start(monkeypatch):
    import x_keyword_ui as ui
    monkeypatch.setattr(sys, "argv", ["ui", "--doctor"])
    monkeypatch.setattr(ui, "diagnose_runtime", lambda root: {"ok": True, "checks": []})
    monkeypatch.setattr(ui, "print_diagnostics", lambda report: 0)
    monkeypatch.setattr(ui, "ThreadingHTTPServer", lambda *a: pytest.fail("server forbidden"))
    assert ui.main() == 0


def test_ui_rejects_remote_binding_before_server_start(monkeypatch):
    import x_keyword_ui as ui
    monkeypatch.setattr(sys, "argv", ["ui", "--host", "0.0.0.0"])
    monkeypatch.setattr(ui, "ThreadingHTTPServer", lambda *a: pytest.fail("server forbidden"))
    with pytest.raises(SystemExit) as exc:
        ui.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("headers, expected", [
    ({"Origin": "https://example.invalid", "Content-Type": "application/json"}, 403),
    ({"Origin": "null", "Content-Type": "application/json"}, 403),
    ({"Origin": "http://127.0.0.1:1", "Content-Type": "application/json"}, 403),
    ({"Content-Type": "text/plain"}, 415),
    ({"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json"}, 403),
])
def test_ui_rejects_cross_site_writes(headers, expected, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    import x_keyword_ui as ui
    monkeypatch.setattr(ui, "stop_all_jobs", lambda: pytest.fail("must not stop tasks"))
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/stop-all", data=b"{}", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ui_accepts_local_json_request(monkeypatch):
    import threading
    import urllib.request
    import x_keyword_ui as ui
    monkeypatch.setattr(ui, "stop_all_jobs", lambda: {"stopped": []})
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(url + "/api/stop-all", data=b"{}", headers={"Origin": url, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
