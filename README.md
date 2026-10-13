# X Research Workbench

本地 X/Twitter 科研采集工具。**2026-10-13整理的源码版本**：真实 SearchTimeline 响应、精确日期区间、原子检查点、游标续采、原始响应证据及受限404复核。

这个仓库只发布代码、测试和说明。**没有你的帖子数据、Cookie、浏览器登录目录或采集断点**；克隆仓库不会把旧任务或账号带到新电脑。

## 先看这些文件

|用途|文件|说明|
|---|---|---|
|核心爬虫|[tools/x_keyword_collector.py](tools/x_keyword_collector.py)|查询构造、响应匹配、解析、翻页、逐页保存、统一结束判定|
|网页工作台|[tools/x_keyword_ui.py](tools/x_keyword_ui.py)|账号管理、采集设置、任务查看和导出；不是底层爬虫|
|独立断点续采|[tools/x_continue_saved_cursors.py](tools/x_continue_saved_cursors.py)|核验计划和源文件，从已保存cursor继续；与普通工作台任务分开|
|404统一规则|[tools/x_recovery_policy.py](tools/x_recovery_policy.py)|有限复核、退避、保护条件；[规则说明](RECOVERY_RULES.md)|
|搜索末端分类|[SEARCH_STATUS_RULES.md](SEARCH_STATUS_RULES.md)|哪些是本次查询末端，哪些只是超时、异常或人工暂停|
|TXT导出|[tools/x_export_column_txt.py](tools/x_export_column_txt.py)|UTF-8、制表符分栏；当前main绑定历史五批，通用导出函数为export_rows|
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

## 什么才叫搜索到底

- `response_end`：当前精确查询成功响应、query/cursor匹配，有效timeline没有下一Bottom游标，且原始响应证据完整。
- `validated_empty_tail`：已有有效样本，至少三个独立成功规范空页，query/cursor链和原始响应哈希完整。
- 40条、100页、连续无新增、重复cursor、翻页超时、404、进程退出、finalized标签均**不能单独证明到底**。

两种已核验末端都只描述该账号该次精确查询，不证明X平台所有帖子，也不保证再次查询返回相同ID集合。研究复现需保留查询、时区、排序、账号节点、时间、停止原因及响应证据。

## 哪些脚本不能直接双击运行

`x_plan_cursor_continuation.py`、`x_guard_audit.py`、`x_snapshot_cursor_continuation.py`、`x_rollout_recovery_policy.py`等包含本研究五批run_id和日期目录，是**已有研究的维护工具**，不是新电脑通用入口。必须先有对应原始数据、计划、证据及用户授权，不能在空目录中盲跑。同步源码不表示五个旧运行实例已加载新代码，也不自动恢复任何定时任务。

## 测试与版本

```powershell
.venv\Scripts\python.exe -m pytest tests/test_x_keyword_collector.py tests/test_x_keyword_ui.py tests/test_x_search_empty_tail.py tests/test_x_cursor_continuation.py tests/test_x_404_recovery.py tests/test_x_bounded_404_retry.py tests/test_x_recovery_policy_v2.py tests/test_x_guard_audit.py -q
```

浏览器/HTTP交接实验另有可选依赖，不混入日常安装。源码来源与SHA256清单见 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)。旧版本通过Git历史查询，不需要另建“新版/最终版/最终最终版”源码目录。

本机运行主目录仍为 `project/`；`x-research-workbench/` 是发布镜像。今后的开发先在主目录验证，再有选择地同步源码，不能把整份研究目录上传。
