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
