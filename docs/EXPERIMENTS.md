# 主程序与历史实验分开看

日常启动只使用根目录 `打开X关键词帖子采集器.cmd` 或 `tools/x_keyword_ui.py`，底层核心是 `tools/x_keyword_collector.py`。

## 已纳入新版源码的研究维护工具

- `x_continue_saved_cursors.py`、`x_recovery_policy.py`、`x_probe_continuation_404.py`、`x_apply_404_recovery.py`：当前受控游标续采/404证据核验。
- `x_plan_cursor_continuation.py`、`x_guard_audit.py`、`x_snapshot_cursor_continuation.py`：本研究五批台账和审计，路径及run_id并非通用模板。
- `x_rollout_recovery_policy.py`：一次性边界切换工具；会操作核验过的所属进程，不是心跳或普通启动器。未经明确部署授权不要运行。
- `x_run_quality_supplement.py`、`x_prepare_staged_collection.py`：10月8日分阶段独立计划流程，依赖本机历史数据与计划。
- `x_screen_quality.py`、`x_quality_review.py`：正文命中和技术异常筛查，不自动认定无关或全量。
- `x_verify_search_bottom.py`、`x_probe_terminal_cursor.py`、`x_compare_ab_verification.py`、`x_record_experiment_tails.py`：AB末端对照/证据复核实验；不能把另一日证据借给当前日期。

## 旧浏览器/HTTP交接实验

历史已有 `x_browserless_probe.py`、`x_browserless_handoff.py` 和第六账号HTTP实验启动器。这些保留在仓库是为了追溯，**不是当前五账号采集主路线**。依赖在 `requirements-browserless.txt`、`requirements-handoff.txt`，无需为了日常工作台安装。

实验必须用独立自有账号和单独输出目录，不读取/共享A–E登录资料，不绕过验证或速率限制。旧实验可行性说明不代表今天仍可运行。

`PROGRESS.md`是历史记录；最新入口和文件角色以根目录README、状态规则与恢复规则为准。旧源码保存在Git历史中，不删除其开发来历。
