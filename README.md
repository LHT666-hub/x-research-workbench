# X Research Workbench

本地 X/Twitter 科研采集工具。**2026-10-15模块化与权威检查点整合（0.3.0）**：真实 SearchTimeline 响应、精确日期区间、原子检查点、游标续采、原始响应证据及受限404复核；组件拆分、阶段计时、可选增量检查点和统一读取。

这个仓库只发布代码、测试和说明。**没有你的帖子数据、Cookie、浏览器登录目录或采集断点**；克隆仓库不会把旧任务或账号带到新电脑。

## 先看这些文件

|用途|文件|说明|
|---|---|---|
|核心协调/兼容入口|[tools/x_keyword_collector.py](tools/x_keyword_collector.py)|保持旧CLI/import兼容，组织分页和既有功能|
|模块地图|[docs/MODULAR_ARCHITECTURE.md](docs/MODULAR_ARCHITECTURE.md)|查询、解析、政策、传输、存储、证据缓存和计时已拆入tools/x_workbench/|
|网页工作台|[tools/x_keyword_ui.py](tools/x_keyword_ui.py)|账号管理、采集设置、任务查看和导出；不是底层爬虫|
|独立断点续采|[tools/x_continue_saved_cursors.py](tools/x_continue_saved_cursors.py)|核验计划和源文件，从已保存cursor继续；与普通工作台任务分开|
|只读检查点查看|[tools/x_read_checkpoint.py](tools/x_read_checkpoint.py)|识别权威SQLite或旧JSON，不启动浏览器/采集|
|404统一规则|[tools/x_recovery_policy.py](tools/x_recovery_policy.py)|有限复核、退避、保护条件；[规则说明](RECOVERY_RULES.md)|
|搜索末端分类|[SEARCH_STATUS_RULES.md](SEARCH_STATUS_RULES.md)|哪些是本次查询末端，哪些只是超时、异常或人工暂停|
|TXT导出|[tools/x_export_column_txt.py](tools/x_export_column_txt.py)|UTF-8、制表符分栏；main绑定历史五批，export_checkpoint读取权威版本并生成回执|
|独立续采审计|[tools/x_audit_continuations.py](tools/x_audit_continuations.py)|同时核对数据库/JSON、结束证据、计划与真实进程；只读|
|数据保存位置|[docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md)|原始批次、补采、续采、导出、证据不能混为一个文件|
|历史/实验代码|[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)|历史脚本的适用范围；不作为日常启动入口|

## 日常启动

Windows、Python 3.11+、安装在标准位置的Google Chrome。

```powershell
git clone https://github.com/LHT666-hub/x-research-workbench.git
cd x-research-workbench
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\x_keyword_ui.py
```

也可以依次双击 `安装依赖.cmd` 和 `打开X关键词帖子采集器.cmd`。默认地址：<http://127.0.0.1:8766/>。需要你在自己的电脑正常登录；不绕过验证码、登录或限流。换电脑后必须重新登录。

核心Chrome路径当前在 `tools/x_account_collector.py` 的 `CHROME` 常量中；非标准安装路径需要按实际路径调整，不能只设置未被代码读取的环境变量。

当前研究方案参数为 `graphql / deferred / pagination_trigger_attempts=3 / pagination_trigger_timeout_ms=15000 / coverage_first`。正文是否含关键词与搜索返回是否相关是不同指标；不能只用“搜到了”证明正文命中。作者所在地补全是单独功能，默认研究方案不补查作者位置。

## 本次改造的边界

- **默认仍为浏览器签名路线，仍用cursor翻页**；安装或更新代码不会自动恢复任务、迁移账号或转换旧数据。研究任务恢复须另行明确授权并通过检查。
- 新增`timings.json`，区分请求等待、解析、保存、证据核验与合法冷却；嵌套阶段不可直接相加当总耗时。
- 实时状态可缓存已核验证据；最终结束验收仍重新读取实际证据并检查SHA256，不放宽“到底”政策。
- 独立续采支持可选`--checkpoint-backend sqlite`，帖子和cursor同事务增量提交；CLI默认JSON不变。启用后SQLite为权威、JSON快照可能滞后。当前工作台、续采审计与TXT导出通过统一入口读取，返回版本号；数据库异常会明确报错，不退回旧JSON伪装最新。历史维护脚本仍须先核兼容性，见[兼容与恢复说明](docs/MODULAR_ARCHITECTURE.md)。不是对旧五批的自动转换。
- 工作台新增独立续采只读面板，展示实际新增数、当前日期/页码、证据核验末端数和真实worker；原任务按钮不控制独立worker。每次TXT导出记录来源版本、行数和文件哈希，不把续采新增数当历史合计。
- 直接HTTP实验接口已与同一分页/结束政策连接，**默认禁止请求，未接入正式UI/CLI，尚未完成真实签名/会话与速度验证**。不是安装后便自动无浏览器；不直接接入twscrape、不轮换账号、不绕限流。
- `tools/x_offline_checkpoint_benchmark.py`仅用临时合成数据测试存储，不登录、不请求X，不能作为实网倍速证明。
- 第二轮减少了逐页全列表扫描/复制、按日期反复全量筛选、同页证据重复落盘、首响应重复解析及队列重复构造；搜索前仅阶段变化时不重建CSV/HTML/ZIP。新增独占耗时`self_seconds`，避免嵌套计时造成误读。这些不降低限流等待，不放宽证据核验。

## 什么才叫搜索到底

- `response_end`：当前精确查询成功响应、query/cursor匹配，有效timeline没有下一Bottom游标，且原始响应证据完整。
- `validated_empty_tail`：已有有效样本，至少三个独立成功规范空页，query/cursor链和原始响应哈希完整。
- 40条、100页、连续无新增、重复cursor、翻页超时、404、进程退出、finalized标签均**不能单独证明到底**。

两种已核验末端都只描述该账号该次精确查询，不证明X平台所有帖子，也不保证再次查询返回相同ID集合。研究复现需保留查询、时区、排序、账号节点、时间、停止原因及响应证据。

## 哪些脚本不能直接双击运行

`x_plan_cursor_continuation.py`、`x_guard_audit.py`、`x_snapshot_cursor_continuation.py`、`x_rollout_recovery_policy.py`等包含本研究五批run_id和日期目录，是**已有研究的维护工具**，不是新电脑通用入口。必须先有对应原始数据、计划、证据及用户授权，不能在空目录中盲跑。同步源码不表示五个旧运行实例已加载新代码，也不自动恢复任何定时任务。

## 测试与版本

```powershell
.venv\Scripts\python.exe -m pytest tests/test_x_efficiency_regressions.py tests/test_x_workbench_components.py tests/test_x_keyword_collector.py tests/test_x_keyword_ui.py tests/test_x_search_empty_tail.py tests/test_x_cursor_continuation.py tests/test_x_404_recovery.py tests/test_x_bounded_404_retry.py tests/test_x_recovery_policy_v2.py tests/test_x_guard_audit.py -q
.venv\Scripts\python.exe -m pytest tests/test_x_authoritative_readers.py tests/test_x_verified_resume.py -q
```

浏览器/HTTP交接实验另有可选依赖，不混入日常安装。源码来源与SHA256清单见 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)。旧版本通过Git历史查询，不需要另建“新版/最终版/最终最终版”源码目录。

本机运行主目录仍为 `project/`；`x-research-workbench/` 是发布镜像。今后的开发先在主目录验证，再有选择地同步源码，不能把整份研究目录上传。
