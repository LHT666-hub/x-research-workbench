# 贡献与开发

欢迎改进安装体验、稳定性、可审计性和文档。当前尚未选定开源许可证，提交/复用前请先与维护者确认许可范围；本次公开源码不替代许可证选择。

## 安全开发

不要操作维护者账号、既有任务、浏览器 profile 或研究输出。使用独立目录、合成数据、本地临时网页；不为测试启动真实采集、清 guard_hold、缩短 reset 或同账号并发。

模块地图见 docs/MODULAR_ARCHITECTURE.md。分页、去重、日期、原始证据、保护和存储须有回归测试，不能只改标签使其“完整”。

~~~powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
~~~

浏览器测试只访问合成本地页面，需要可发现的 Chrome。CI 使用 Playwright 安装的隔离 Chromium，通过 X_WORKBENCH_CHROME 指定，不需要 X 账号或仓库 Secrets。不要把真实账号作为自动测试依赖。

PR 说明变更、复现测试、对旧 JSON/SQLite、结束规则和断点的影响。区分局部离线性能和实网端到端性能。

## 发布检查

1. 回归与全新目录启动检查通过，没有后台真实采集被启动。
2. README、环境变量、版本和变更记录一致。
3. 检查当前 Git、可达历史、分支/标签、附件与 Actions，不含凭证和数据。
4. SOURCE_SNAPSHOT.json 对应发布源码，不含输出/会话。
5. 推送后核对远端提交；改可见性须所有者明确授权。
6. 许可证单独决定，不因 public 自动添加授权条款。
