# X Research Workbench

**面向研究的本地 X/Twitter 数据采集与审计工作台。**

关键词与日期检索、逐页安全保存、精确游标续采、原始响应证据、明确停止原因和分栏导出。重点是能够说明：**查询了什么、保存了什么、为什么停止、哪些结论有证据。**

A local-first research workbench for X/Twitter: keyword/date search, durable checkpoints, cursor continuation, raw-response evidence, explicit stop states, and auditable exports. Windows-first; uses your own normally signed-in Chrome session.

> 当前版本 **0.3.1**。研究工具，仍在迭代；不是 X 官方产品，不保证平台全量、永久接口兼容或再次查询结果完全一致。默认使用 Chrome，不是免登录 HTTP 爬虫。

## 从哪里开始

- 第一次使用：[新手安装与首个任务](docs/QUICKSTART.md)
- 无法启动、没有下一页、数量偏少：[常见问题](docs/TROUBLESHOOTING.md)
- 怎么算“到底”：[搜索结束判据](SEARCH_STATUS_RULES.md)
- 找代码和数据：[模块地图](docs/MODULAR_ARCHITECTURE.md) · [数据布局](docs/DATA_LAYOUT.md)
- 更新、贡献与安全：[版本记录](CHANGELOG.md) · [贡献说明](CONTRIBUTING.md) · [安全说明](SECURITY.md)

## 核心能力与边界

| 能力 | 已实现 | 需要注意 |
|---|---|---|
| 关键词、日期与排序 | Latest / Top、指定时区、日期切片 | 搜索返回相关内容，不等于每条正文都含关键词 |
| 分页取数 | 捕获真实 SearchTimeline JSON，匹配 query 与请求 cursor | Chrome 网页生成请求，不是不受限制的官方全量 API |
| 安全保存 | 页级保存帖子与断点，JSON 原子替换 | 可选 SQLite 目前用于独立游标续采，不是全仓库自动迁移 |
| 断点续采 | 校验查询、源文件、保护状态和已保存 cursor | 查询不同不能直接借用旧游标，缺断点不能假装续采 |
| 搜索末端证据 | 成功无下一游标响应、三次规范空尾页、原始证据哈希 | 只描述该次精确搜索，不证明全平台所有帖子 |
| 多账号节点 | 会话隔离、管理与任务状态 | 同一节点不能并发两个任务，不轮换账号绕限流 |
| 可审计输出 | 停止原因、日期记录、原始响应、阶段计时 | 页数、帖子数、进程退出或 finalized 本身不等于到底 |
| 分析与导出 | JSON、CSV、UTF-8 制表符分栏 TXT，部分流程有 HTML / ZIP | 独立续采导出仅含该轮新增，不冒充历史合并总量 |
| 作者信息 | 保留响应已有字段，支持独立所在地补查 | 自填位置与 Account based in 独立；默认研究方案不额外补查 |
| 无浏览器 HTTP | 隔离实验接口、离线测试 | 未接入默认流程，尚无同条件实网速度优越性结论 |

## 快速安装（Windows）

需要 **Python 3.11+、Google Chrome**，以及可正常访问 X 的网络和自己的登录账号。下载 ZIP 后完整解压，不要在压缩包内双击运行。

~~~powershell
git clone https://github.com/LHT666-hub/x-research-workbench.git
cd x-research-workbench
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\x_doctor.py
.venv\Scripts\python.exe tools\x_keyword_ui.py
~~~

也可以双击 **安装依赖.cmd → 检查运行环境.cmd → 打开X关键词帖子采集器.cmd**。默认地址：<http://127.0.0.1:8766/>。

- 自检不启动浏览器、不请求 X、不读取 Cookie；通过自检不代表登录或采集成功。
- 首次按提示在自己的 Chrome 窗口正常登录；仓库没有“现成账号”。
- Chrome 自动尝试系统安装、用户安装与 PATH。非标准安装可在启动前配置：

~~~powershell
$env:X_WORKBENCH_CHROME = 'D:\Apps\Chrome\Application\chrome.exe'
.venv\Scripts\python.exe tools\x_doctor.py
.venv\Scripts\python.exe tools\x_keyword_ui.py
~~~

显式路径无效时不会静默回退。临时变量须在启动程序的同一终端设置。正式采集使用本机 Chrome，通常不需要另外下载 Playwright Chromium；CI 使用独立浏览器测试合成页面，不登录 X。

当前只验收 Windows 启动流程，不宣称 macOS/Linux 可直接双击使用。工作台没有远程访问鉴权，主入口只允许本机绑定：**公开代码不等于把本地服务开放到公网**。

## 第一个任务怎么验收

先用一个关键词、一个日期、小样本模式确认登录、解析、日期和输出正常，再设置正式范围、切片和限额。达到小样本条数/页数上限只证明流程可用，不证明该日完整。

界面开始与结束日期均包含当天。内部按所选时区，生成开始日 00:00 至结束日次日 00:00 的半开时间戳区间。例如仅采 2026-06-25，界面两项都填 2026-06-25；这不等于手工向 X 提交同日 since/until 查询。

研究方案常用 graphql / deferred / 分页触发 3 次、每次 15000ms / coverage_first。覆盖优先允许保留已有样本但中断的日期，因此必须同时看停止原因，不可把“覆盖完成”称为“搜索到底”。

## 怎样区分“到底”和异常

- **response_end**：成功响应匹配当前查询与请求游标，有效 timeline 没有下一 Bottom 游标，并有完整原始证据。
- **validated_empty_tail**：已有有效样本，至少三个独立成功规范空页，查询、cursor 链、日期校验与原始响应哈希均通过。
- **待核验或未完成**：分页超时、网络错误、404、重复 cursor、仅无新增、限额、人工暂停、未知中断。

“成功空页”不同于没收到响应。HTTP 200 也不够，必须验证结构、query 和 cursor。401/403、登录/验证码、日期不匹配、磁盘失败仍受保护；429 与 reset 必须等待。受限 404 复核不是无限重试。

旧记录缺原始证据，不能只改标签补成完整。结果可能随时间、账号可见性和平台索引变化；重复性必须保留查询、时区、排序、账号节点、时间、停止原因与响应证据。

## 数据和账号在哪里

本仓库只发布代码、合成测试和说明，**不包含帖子数据、Cookie、密码、登录目录或研究检查点**。克隆不会继承作者的五批历史任务。

新普通任务保存在本项目 outputs/x_keyword_runs/<run_id>/，会话保存在 tmp/。JSON / 显式启用的 SQLite 是结构化主数据；TXT 是分析导出，不是恢复依据。独立续采、实验、历史快照分别保存，详见[数据布局](docs/DATA_LAYOUT.md)。

TXT 使用 UTF-8 BOM 和制表符分栏，一行一帖，正文换行转义为 \n。Excel 导入请选择 UTF-8、制表符，并将帖子 ID 按文本读取。备份运行中的 SQLite 不能忽略 WAL；安全停止后整体备份，或使用 SQLite backup。

**不要将 outputs/、tmp/、日志、抓包或完整问题压缩包上传 GitHub。**

## 源码入口

| 文件/目录 | 作用 |
|---|---|
| tools/x_keyword_ui.py | 本地工作台、账号、普通任务、历史与独立续采只读视图 |
| tools/x_keyword_collector.py | 兼容 CLI、分页协调与采集逻辑 |
| tools/x_workbench/ | 查询、解析、传输、证据、存储、统一读取和计时模块 |
| tools/x_doctor.py | 无网络环境自检，可加 --json |
| tools/x_continue_saved_cursors.py | 已有计划的精确游标续采，不是新用户一键入口 |
| tools/x_read_checkpoint.py | 按权威版本查看 JSON / SQLite |
| tools/x_export_column_txt.py | 历史 main 绑定研究批次；export_checkpoint 提供通用权威导出 |
| docs/EXPERIMENTS.md | 研究维护/实验脚本的适用边界 |

含既定 run_id 的规划、修复、切换和恢复脚本依赖本机历史源文件，不是新电脑通用启动器。普通 UI 按钮不控制独立 worker；关闭页面服务不等于所有任务已安全停止。

## 性能、测试与更新

已经优化重复扫描/解析/写盘、证据复用与可选增量保存。计时区分响应等待、正常间隔、限流、解析、保存和核验；限流不会因本地优化消失。

离线存储基准仅衡量合成工作量，**不能换算成真实采集倍速**。默认仍使用 Chrome；没有同条件实网对照，不宣称比 twscrape 更快。

~~~powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe tools\x_keyword_ui.py --version
.venv\Scripts\python.exe tools\x_doctor.py --json
~~~

测试使用合成数据，少数通过浏览器打开本地测试页面，不应登录或请求 X。CI 运行回归测试，通过不代表真实账号与平台接口永远可用。

更新前先区分任务类型，安全落盘并核实所属 worker 退出，备份自己的输出和会话，再更新。不要强关 Chrome、清空数据或重置 Git 来代替安全停止。新版不会自动恢复任务、转换旧数据或开启定时采集。

## 公开发布与许可

本项目从自建科研流程演化，保留研究维护脚本用于追溯。独立 twscrape fork 是另一个仓库，未直接接入本仓库默认路线。

当前尚未选择开源许可证；public 只表示源码可见，不代表本次替作者选定第三方使用、再分发或商业授权条款。贡献前请先讨论许可。遵守 X 访问要求、适用规则和所在机构的研究伦理；公开帖子也可能含个人信息。

反馈请提供版本、操作步骤、脱敏停止原因与自检结论，不要发送密码、Cookie、Authorization、完整抓包或真实研究数据。见[安全说明](SECURITY.md)。
