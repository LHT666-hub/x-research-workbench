from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import subprocess
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from x_account_collector import normalize_username


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
COLLECTOR = ROOT / "tools" / "x_account_collector.py"
OUTPUT_ROOT = ROOT / "outputs" / "x_account_runs"
MAX_ACCOUNTS = 1000


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>X账号所在地采集器</title><style>
:root{--navy:#152637;--blue:#1677ff;--line:#d8e2eb;--muted:#687b8d;--bg:#f3f6f9;--green:#16834b}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:"Microsoft YaHei",system-ui,sans-serif;color:#172033}main{max-width:1240px;margin:auto;padding:25px}.hero{background:linear-gradient(120deg,#0f1c28,#244b68);color:white;border-radius:15px;padding:25px 29px;margin-bottom:18px}.hero h1{margin:0 0 8px;font-size:28px}.hero p{margin:0;color:#dce8f1;line-height:1.6}.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px}@media(max-width:900px){.grid{grid-template-columns:1fr}}.panel{background:white;border:1px solid var(--line);border-radius:12px;padding:20px}.panel h2{margin:0 0 13px;font-size:19px}.step{color:var(--blue);font-size:13px;margin-right:5px}.sub{color:var(--muted);font-size:13px;line-height:1.6}textarea{width:100%;min-height:330px;resize:vertical;padding:13px;border:1px solid #aebdca;border-radius:8px;font:14px/1.55 Consolas,"Microsoft YaHei",monospace}textarea:focus,input:focus,select:focus{outline:2px solid #a8ccff;border-color:var(--blue)}.row{display:flex;gap:11px;align-items:center;flex-wrap:wrap;margin-top:12px}.row label{display:flex;align-items:center;gap:6px;font-size:14px}input[type=number],select{padding:8px 9px;border:1px solid #aebdca;border-radius:7px;background:white}.buttons{display:flex;gap:9px;flex-wrap:wrap;margin-top:15px}button,.result{border:0;border-radius:8px;padding:10px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}.primary{background:var(--blue);color:white}.secondary{background:#e6eef5;color:#17324d}.danger{background:#ffe8e8;color:#a11}.success{background:#e1f5e9;color:#116d3d}button:disabled{opacity:.48;cursor:not-allowed}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:13px 0}.stat{background:#f1f6fa;border-radius:8px;padding:11px;text-align:center}.stat b{display:block;font-size:21px;color:#174c73}.stat span{font-size:12px;color:var(--muted)}.status{display:flex;align-items:center;gap:9px;padding:11px 13px;border-radius:8px;background:#eef4f8;margin-bottom:12px}.dot{width:10px;height:10px;border-radius:50%;background:#8aa0b3}.dot.running{background:#1677ff;box-shadow:0 0 0 5px #dcecff}.dot.success{background:#1a9b53}.dot.failed{background:#d73737}.dot.stopped{background:#e18a16}pre{height:430px;overflow:auto;background:#101820;color:#d8e7f2;padding:14px;border-radius:9px;white-space:pre-wrap;word-break:break-word;font:13px/1.55 Consolas,monospace}.warn{background:#fff8df;border:1px solid #f0d993;color:#7c5700;padding:10px 12px;border-radius:8px;font-size:13px;line-height:1.55;margin-top:12px}.hidden{display:none!important}.fields{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.fields span{background:#edf4fa;border:1px solid #d4e3ef;border-radius:14px;padding:5px 8px;font:12px Consolas,monospace}</style></head><body><main>
<section class="hero"><h1>X / Twitter 账号所在地采集器</h1><p>批量读取 About this account 的 Account based in，并同时保存数字用户 ID、Profile Location、验证状态和公开账号资料。全程使用你自己的正常登录会话。</p></section>
<div class="grid"><section class="panel"><h2><span class="step">01</span>输入账号</h2><textarea id="accounts" placeholder="每行一个，例如：&#10;@leige88888&#10;elonmusk&#10;https://x.com/NASA"></textarea>
<div class="buttons"><button class="secondary" id="example">载入示例</button><button class="secondary" id="clear">清空</button></div><div class="stats"><div class="stat"><b id="count">0</b><span>去重账号</span></div><div class="stat"><b id="estimate">—</b><span>预计耗时</span></div><div class="stat"><b id="requests">0</b><span>核心请求约数</span></div></div>
<h2><span class="step">02</span>运行设置</h2><div class="row"><label>模式 <select id="mode"><option value="smoke">冒烟：前5个</option><option value="all">全部账号</option></select></label><label>账号间隔 <input id="delayMin" type="number" min="2" max="300" value="8">—<input id="delayMax" type="number" min="2" max="300" value="15"> 秒</label></div>
<div class="warn">首次运行会打开独立的研究 Chrome。请在其中正常登录 X，不要把密码粘贴到本页面。遇到验证码、401、403 或 429 时，工具会停止并保留断点，不会尝试绕过。</div>
<div class="buttons"><button class="primary" id="start">开始采集</button><button class="danger" id="stop" disabled>停止并保留断点</button><button class="success" id="resume" disabled>继续上次任务</button></div>
<h2 style="margin-top:20px"><span class="step">字段</span>自动输出</h2><div class="fields"><span>username</span><span>user_id</span><span>account_based_in</span><span>profile_location</span><span>source</span><span>connected_via</span><span>followers_count</span><span>following_count</span><span>posts_count</span><span>verified</span><span>bio</span></div>
</section><section class="panel"><h2><span class="step">03</span>运行与结果</h2><div class="status"><span class="dot" id="dot"></span><strong id="status">空闲</strong></div><div id="output" class="sub">尚未开始。</div><div class="buttons"><a class="result primary hidden" id="result" target="_blank">打开可筛选结果</a></div><label class="sub" for="log">实时日志</label><pre id="log">等待任务……</pre></section></div></main>
<script>const el=id=>document.getElementById(id),accounts=el('accounts'),start=el('start'),stop=el('stop'),resume=el('resume'),result=el('result'),log=el('log');accounts.value=localStorage.getItem('xAccountInputs')||'';
function parse(){const seen=new Set(),out=[];for(let line of accounts.value.split(/\r?\n/)){line=line.trim();if(!line||line.startsWith('#'))continue;let v=line;if(/^https?:\/\//i.test(v)){try{v=new URL(v).pathname.split('/').filter(Boolean)[0]||''}catch{}}v=v.replace(/^@/,'');if(/^[A-Za-z0-9_]{1,15}$/.test(v)&&!seen.has(v.toLowerCase())){seen.add(v.toLowerCase());out.push(v)}}return out}
function update(){const n=parse().length,avg=(Number(el('delayMin').value)+Number(el('delayMax').value))/2;el('count').textContent=n;el('requests').textContent=n*2;const seconds=n*(avg+6);el('estimate').textContent=!n?'—':seconds<3600?Math.ceil(seconds/60)+'分钟':(seconds/3600).toFixed(1)+'小时';localStorage.setItem('xAccountInputs',accounts.value)}accounts.oninput=update;el('delayMin').oninput=update;el('delayMax').oninput=update;el('example').onclick=()=>{accounts.value='@leige88888\nNASA\nelonmusk';update()};el('clear').onclick=()=>{if(confirm('清空输入吗？')){accounts.value='';update()}};
async function api(path,options={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...options}),d=await r.json();if(!r.ok)throw new Error(d.error||'请求失败');return d}start.onclick=async()=>{const list=parse();if(!list.length)return alert('请先输入有效的X用户名或主页URL。');try{await api('/api/start',{method:'POST',body:JSON.stringify({accounts:list,mode:el('mode').value,delay_min:Number(el('delayMin').value),delay_max:Number(el('delayMax').value)})});refresh()}catch(e){alert(e.message)}};stop.onclick=async()=>{if(confirm('停止当前任务并保留断点吗？'))try{await api('/api/stop',{method:'POST',body:'{}'})}catch(e){alert(e.message)}};resume.onclick=async()=>{try{await api('/api/resume',{method:'POST',body:'{}'});refresh()}catch(e){alert(e.message)}};
async function refresh(){try{const s=await api('/api/status');el('status').textContent=s.label;el('dot').className='dot '+s.status;start.disabled=s.status==='running';stop.disabled=s.status!=='running';resume.disabled=s.status==='running'||!s.can_resume;log.textContent=s.logs.length?s.logs.join('\n'):'等待任务……';log.scrollTop=log.scrollHeight;el('output').textContent=s.output_dir?'输出目录：'+s.output_dir:'尚未开始。';if(s.result_url){result.href=s.result_url;result.classList.remove('hidden')}else result.classList.add('hidden')}catch(e){el('status').textContent='页面服务连接失败'}}update();setInterval(refresh,1000);refresh();</script></body></html>"""


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def normalize_accounts(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("账号列表格式无效")
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        username = normalize_username(str(raw))
        if username and username.casefold() not in seen:
            seen.add(username.casefold())
            output.append(username)
    if not output:
        raise ValueError("没有读取到有效账号")
    if len(output) > MAX_ACCOUNTS:
        raise ValueError(f"单个任务最多 {MAX_ACCOUNTS} 个账号")
    return output


def resolve_result(relative: str) -> Path:
    target = (OUTPUT_ROOT / relative).resolve()
    if not target.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("非法结果路径")
    return target


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.status = "idle"
        self.label = "空闲"
        self.logs: list[str] = []
        self.output_dir = self._latest_incomplete()
        self.result_url = self._result_url(self.output_dir)
        if self.output_dir:
            self.label = "空闲（可继续最近任务）"

    @staticmethod
    def _result_url(directory: Path | None) -> str:
        if not directory or not (directory / "index.html").exists():
            return ""
        return "/results/" + urllib.parse.quote(directory.relative_to(OUTPUT_ROOT).as_posix()) + "/index.html"

    @staticmethod
    def _incomplete(directory: Path) -> bool:
        config = read_json(directory / "run_config.json", {})
        raw = read_json(directory / "raw.json", {})
        return isinstance(config, dict) and isinstance(raw, dict) and int(config.get("account_count", 0)) > len(raw.get("records", []))

    @classmethod
    def _latest_incomplete(cls) -> Path | None:
        if not OUTPUT_ROOT.exists():
            return None
        return next((p for p in sorted(OUTPUT_ROOT.iterdir(), reverse=True) if p.is_dir() and cls._incomplete(p)), None)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"status": self.status, "label": self.label, "logs": self.logs[-1500:], "output_dir": str(self.output_dir or ""), "result_url": self.result_url, "can_resume": bool(self.output_dir and self._incomplete(self.output_dir) and self.status != "running")}

    def _command(self, output_dir: Path, config: dict[str, Any], resume: bool) -> list[str]:
        command = [str(PYTHON), str(COLLECTOR), "--accounts-file", str(output_dir / "input_accounts.txt"), "--output", str(output_dir), "--delay-min", str(config["delay_min"]), "--delay-max", str(config["delay_max"])]
        if resume:
            command.append("--resume")
        return command

    def _launch(self, output_dir: Path, config: dict[str, Any], resume: bool = False) -> None:
        self.status, self.label = "running", ("正在继续采集" if resume else "正在采集")
        if not resume:
            self.logs = []
        self.logs.extend([f"{'继续' if resume else '启动'}任务：{config['account_count']} 个账号", f"输出目录：{output_dir}"])
        self.output_dir, self.result_url = output_dir, ""
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.process = subprocess.Popen(self._command(output_dir, config, resume), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=creationflags)
        threading.Thread(target=self._read_output, daemon=True).start()

    def start(self, accounts: list[str], delay_min: float, delay_max: float) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有采集任务正在运行")
            run_name = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = OUTPUT_ROOT / run_name
            output_dir.mkdir(parents=True, exist_ok=False)
            (output_dir / "input_accounts.txt").write_text("\n".join(accounts) + "\n", encoding="utf-8")
            config = {"account_count": len(accounts), "delay_min": delay_min, "delay_max": delay_max}
            (output_dir / "run_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            self._launch(output_dir, config)

    def resume(self) -> None:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有采集任务正在运行")
            if not self.output_dir or not self._incomplete(self.output_dir):
                raise RuntimeError("没有可继续的未完成任务")
            config = read_json(self.output_dir / "run_config.json", {})
            if not isinstance(config, dict):
                raise RuntimeError("任务配置损坏")
            self._launch(self.output_dir, config, True)

    def _read_output(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            with self.lock:
                self.logs.append(line.rstrip())
                self.logs = self.logs[-1800:]
        code = process.wait()
        with self.lock:
            if self.status == "stopped":
                self.label = "已停止（断点已保留）"
            elif code == 0:
                self.status, self.label = "success", "采集完成"
            else:
                self.status, self.label = "failed", f"运行失败（退出码 {code}）"
            self.result_url = self._result_url(self.output_dir)

    def stop(self) -> None:
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                raise RuntimeError("当前没有运行中的任务")
            self.status, self.label = "stopped", "正在停止"
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()


MANAGER = JobManager()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def send_bytes(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            self.send_bytes(PAGE.encode(), "text/html; charset=utf-8"); return
        if parsed.path == "/api/status":
            self.send_json(MANAGER.snapshot()); return
        if parsed.path.startswith("/results/"):
            try:
                target = resolve_result(urllib.parse.unquote(parsed.path[len("/results/"):]))
                if not target.is_file():
                    raise FileNotFoundError(target)
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_bytes(target.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") else "")); return
            except (ValueError, FileNotFoundError):
                self.send_json({"error": "结果文件不存在"}, HTTPStatus.NOT_FOUND); return
        self.send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            data = self.read_json()
            if self.path == "/api/start":
                accounts = normalize_accounts(data.get("accounts")); mode = str(data.get("mode", "smoke"))
                if mode == "smoke": accounts = accounts[:5]
                delay_min, delay_max = float(data.get("delay_min", 8)), float(data.get("delay_max", 15))
                if not 2 <= delay_min <= delay_max <= 300: raise ValueError("账号间隔必须为 2—300 秒")
                MANAGER.start(accounts, delay_min, delay_max); self.send_json({"ok": True}); return
            if self.path == "/api/resume": MANAGER.resume(); self.send_json({"ok": True}); return
            if self.path == "/api/stop": MANAGER.stop(); self.send_json({"ok": True}); return
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> int:
    parser = argparse.ArgumentParser(description="X账号所在地采集器本地界面")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8766); parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not PYTHON.exists(): raise SystemExit(f"未找到项目 Python：{PYTHON}")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler); url = f"http://{args.host}:{args.port}/"
    print(f"X账号所在地采集器已启动：{url}"); print("保持此窗口打开；按 Ctrl+C 关闭。")
    if not args.no_open: threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n页面服务已关闭。")
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
