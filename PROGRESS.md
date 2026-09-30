# X Research Workbench 进度记录

更新时间：2026-09-30

## 当前实现

1. 使用正常登录会话打开 X 搜索页，并捕获实际 `SearchTimeline` GraphQL JSON。
2. 按关键词、日期片和 Latest/Top 排序采集帖子，保存帖子 ID、正文、时间、作者用户名和稳定用户 ID。
3. 每个日期片保存覆盖状态、停止原因、JSON 页数、耗时和实测速率。
4. `pagination_not_triggered` / `needs_retry` 日期进入独立 `repair_queue.json`，可靠完成的日期不会重复采集。
5. 作者主页自填位置通过 `UserByScreenName` JSON 单独快速补全。
6. `Account based in` 通过 `AboutAccountQuery` 单独慢速补全。
7. 两种所在地拥有独立队列、状态、错误和跨批次缓存。
8. 支持多个账号节点、独立浏览器会话、一键全部暂停和续跑。
9. 支持 CSV、JSON、编码模板、可视化结果页、筛选结果导出和完整 ZIP。

## 数据安全

以下内容不会提交到 GitHub：

- Chrome 登录目录、Cookie 和 CSRF 信息；
- `outputs/` 中的帖子与作者数据；
- 采集日志、断点运行文件和本地配置；
- Network 抓包及包含请求头的调试证据。

## 已知限制

- X 不提供搜索结果总命中数，滚动或游标耗尽不能证明平台数据库绝对全量。
- 高速模式依赖 X 当前网页内部接口，operation hash、字段结构和页面触发机制可能变化。
- 高速翻页会校验响应确属当前关键词与日期片，并用三种页面动作逐级重试；仍未捕获到下一页时记为 `pagination_not_triggered`，不等同于本机断网。
- 已保存最后一个游标用于诊断；跨进程恢复仍按日期片重新进入并按帖子 ID 去重，因为 X 的前端 transaction id 是单次生成，程序不会伪造或绕过它。
- 不绕过登录、验证码、权限控制或速率限制；收到 429 时遵守服务端 reset/退避。

## 验证

本版本在本地通过完整测试套件，并额外验证：

- SearchTimeline JSON 解析与帖子去重；
- 自填位置与 Account based in 字段互不覆盖；
- 两条作者补全队列独立生成；
- 工作台 JavaScript 能正常加载；
- 历史任务、异常日期修复和批量账号控制。
