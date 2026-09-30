# X Research Workbench

面向科研用途的本地 X（Twitter）关键词帖子采集工作台。它使用你自己的正常登录会话，支持通过实际观察到的 `SearchTimeline` JSON 按关键词和日期切片采集帖子，并将作者主页自填位置与 **About this account** 中公开显示的 `Account based in` 分成两条独立补全流程。

## 主要功能

- 关键词与日期范围检索，支持 Latest / Top。
- 高速 JSON 模式解析真实 `SearchTimeline` 响应，同时保留浏览器滚动兼容模式。
- 按天切片、搜索游标翻页、异常日期重跑、断点续跑。
- 可在页面中按需添加、重命名、停用和重新启用账号节点；每个节点拥有相互独立的登录会话。
- 一键暂停全部运行节点并保留各自断点，也可一键继续所有可续跑任务。
- 暖色编辑感研究工作台，包含启用账号、运行中和可续跑任务的实时总览。
- 运行设置按采集范围、时间切片、作者所在地补全和运行节奏分组，高级参数可折叠。
- 完整结果页支持正文、作者、所在地状态、国家/地区、关键词和日期筛选，并可排序、切换每页条数和导出当前筛选。
- 帖子采集和作者补全可以分离：先高速保存帖子、稳定作者 ID 和补全队列。
- “快速补自填位置”使用实际捕获的 `UserByScreenName` JSON；“慢速补 Account based in”使用独立的 `AboutAccountQuery` 队列。
- 两条所在地流程拥有独立状态、错误记录、缓存和断点，不会互相覆盖。
- 帖子与作者去重、历史任务切换、覆盖诊断。
- CSV、JSON、编码模板、可视化页面和 ZIP 一键导出。
- 遇到 HTTP 429 时读取服务端等待信息并进入共享冷却，避免各节点继续冲击所在地接口。

## 2026-09-30 开发进度

- 已完成多账号并行、高速 SearchTimeline JSON、日期切片、覆盖诊断和异常日期修复队列。
- 已完成主页自填位置与 Account based in 的独立补全按钮及 `profile_location_queue.json`。
- 已完成跨批次作者缓存、筛选导出、历史批次切换和批量暂停/续跑。
- 高速翻页只接受当前关键词和日期片的 SearchTimeline 响应，并在页面未触发下一页时逐级重试；仍失败则记为 `pagination_not_triggered`，不等同于电脑断网。该日期片会进入修复队列，已拿到的部分帖子会保留并去重。
- 当前研究数据、登录会话和运行日志只保存在本机；GitHub 仓库只发布程序、测试和说明文档。

## 在另一台 Windows 电脑安装

要求：Python 3.11+、Google Chrome、Git。首先克隆私有仓库：

```powershell
git clone https://github.com/LHT666-hub/x-research-workbench.git
cd x-research-workbench
```

双击 `安装依赖.cmd`，完成后双击 `打开X关键词帖子采集器.cmd`。也可在 PowerShell 中运行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\x_keyword_ui.py
```

工作台默认地址为 <http://127.0.0.1:8766/>。点击“添加/管理账号”即可建立第 5 个或更多账号节点。第一次使用某个节点时，程序会打开独立的 Chrome 窗口；请正常登录自己的 X 账号。不同节点的会话互不共用。

若 Chrome 未安装在标准位置，可先设置：

```powershell
$env:X_CRAWLER_CHROME = "D:\Apps\Google\Chrome\Application\chrome.exe"
```

## 数据与隐私

采集结果保存在 `outputs/`，登录会话和 Cookie 保存在 `tmp/`。这两个目录均被 Git 忽略，不会上传到仓库；换电脑后需要重新登录。不要把 `tmp/` 手动提交或传给别人。

## 使用边界

工具不会绕过登录、验证码、权限控制或速率限制。X 搜索结果页不提供数据库总命中数，因此“滚动到无新增”只表示该会话当时已无法继续获得新结果，不能证明获得平台全部帖子。建议保留输出中的覆盖诊断，并在论文方法部分如实说明采集日期、账号、排序、时间切片和停止规则。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## 第二路线：浏览器登录一次，关闭后用 HTTP 取下一页

当前 Twikit 2.3.3 在 X 的新版 `x-web` 首页上无法完成登录准备，因此不要反复运行下方的 Twikit `login`。`tools/x_browserless_handoff.py` 是独立的替代试验：只为第六账号打开一个**可见**的 Chrome 窗口；你在 X 页面正常登录并自行完成 2FA，程序捕获当前关键词和日期的真实 `SearchTimeline` 请求。浏览器关闭后，它使用同一次会话在 Python 中直接读取**下一页** JSON。这样能实际检验无浏览器的搜索请求是否可行，不猜接口，也不动 A–E。试验收到 429、401、403 或其他 HTTP 错误后不会重试、不会绕过验证。首次只取两页，不能证明全天全量。

本机可在仓库目录直接运行（必须先把已截图泄露的密码修改掉）：

```powershell
py -3 tools\x_browserless_handoff.py --keyword GPT --date 2026-01-01
```

在其他电脑上先安装 `requirements-handoff.txt` 到独立环境。第六账号的 Chrome 登录资料仅留在 Git 忽略的 `tmp/browserless_handoff/`，报告写入 Git 忽略的 `outputs/browserless_handoff/`；不要分享或上传这两个目录。程序不接收终端密码，不保存请求 Cookie、Bearer、CSRF 或完整请求头到报告。若关闭浏览器后的 HTTP 请求失败，报告会明确标记，不能将此方法切换到正式采集。

## 无浏览器搜索试验（隔离，不影响现有批次）

`tools/x_browserless_probe.py` 是针对**另一个自有账号**的小规模可行性试验，不会启动 Chrome，不会读取现有账号的 `tmp/chrome-*` 登录资料，也不会修改 A–E 的断点。它使用 Twikit 2.3.3 作为首个候选传输层；Twikit 是非官方项目，能否在当前 X 接口上成功须由真实账号测试确认。它绝不将试验结果标为“当日全量”。

建议建立独立环境，避免改变正在运行的采集器：

```powershell
py -3 -m venv .venv-browserless
.venv-browserless\Scripts\python.exe -m pip install -r requirements-browserless.txt
```

准备好一个没有被其他采集任务占用的自有账号后，在你自己的电脑终端执行一次正常登录。程序先用 Windows 当前用户的系统代理检查 X 连通性及试验库的无账号登录准备步骤；任一步失败都不会询问账号密码。邮箱、密码和可能出现的 2FA 验证码会在终端中**可见**，请勿截图或共享屏幕；程序不会将密码加入命令行或日志，也不要把凭据发给别人或提交到 GitHub：

```powershell
.venv-browserless\Scripts\python.exe tools\x_browserless_probe.py login
```

如果 X 要求验证器动态码，Twikit 会在首次登录流程中提示你手动输入；不要提供验证器密钥，也不要尝试绕过验证。登录成功后会话只保存在本机被 Git 忽略的 `tmp/browserless_probe/cookies.json`；后续 `probe` 直接读取该文件，正常情况下无需再次输入密码或 2FA。会话失效时须由你重新完成正常登录。不要把会话文件共享给他人。

2026-09-30 的本机无账号诊断表明：系统代理可访问 X，但 Twikit 2.3.3 在解析客户端交易参数的 `get_indices` 步骤失败，尚未到账号或 2FA 阶段。此试验当前未跑通；不要因 `request_failed` 反复输入密码。先修复或替换试验传输层，再进行第六账号验证。

登录成功后，选一日最多试探三页；收到 429、401/403 或 404 就停止，不绕过验证码、权限或额度。可只读对比已有 GPT 批次的帖子 ID：

```powershell
.venv-browserless\Scripts\python.exe tools\x_browserless_probe.py probe --keyword GPT --date 2026-01-01 --max-pages 3 --observed-coverage "C:\Users\LHT\Desktop\Agent\爬虫大法\project\outputs\x_keyword_runs\20260928_122030\coverage_report.csv" --compare-raw "C:\Users\LHT\Desktop\Agent\爬虫大法\project\outputs\x_keyword_runs\20260928_122030\raw.json"
```

Twikit 2.3.3 内置的搜索 query ID 可能过时；上例的 `--observed-coverage` 只读取我们已实际捕获的搜索路径，不读取任何账号 Cookie，也不会猜接口。如果方法或字段已变化，试验应明确失败并保留原采集方式。试验会话默认保存在被 Git 忽略的 `tmp/browserless_probe/cookies.json`，试验结果保存在 `outputs/browserless_probe/`。会话文件等同登录凭据，请只留在本机且不要共享。`profile_location` 是作者自行填写的资料，不是 `Account based in`；本试验不会查询后者。帖子 ID 重合率只能用于检查方法差异，不能证明 X 搜索结果是平台全量。只有确认多页请求、字段、限流处理和内存表现都稳定后，才考虑将该传输层接入正式工作台。
