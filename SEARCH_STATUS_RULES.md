# 统一搜索状态规则

分类入口：`tools/x_keyword_collector.py`的`classify_search_run`。采集、严格续跑判断、审计、界面日期片数、HTML和透明性导出均使用同一套判定。原始`stop_reason`和`coverage_status`保留，不通过改写历史字段伪造完成。

## 任何日期、任何关键词均可达到已核验结束

状态不是6月25日专属。每个账号、关键词、精确查询时间区间、排序单独取得证据。不同日期可以使用同一种规则，但不能借用另一日期的证据或标记；半日结束不能冒充整日结束。

|统一状态|含义|标记搜索到底|
|---|---|---|
|response_end|HTTP200、查询和游标匹配、有效timeline无后续Bottom游标，原始JSON与哈希已核验|是，本次查询响应结束|
|validated_empty_tail|已有有效样本，连续至少3个独立成功响应，cursor链匹配，原始响应仅游标替换而无帖子，未发生日期排除，证据完整|是，本次查询经验空尾部|
|recorded_cursor_end|历史记录称cursor_exhausted，但没有足够原始响应证据|否，待核验|
|in_progress|正在采集或尚未结算的检查点；是否有进程须另查|否|
|empty_tail_candidate=true|发现带游标空响应但核验次数/证据尚不足，是附加诊断标志，不是结束|否|
|no_new_results|连续无新增可能是重复帖或其他情况，保护停止|否|
|repeated_cursor|重复游标防循环停止|否|
|pagination_not_triggered|未取得匹配的下一页响应，不能自动归因断网或采完|否|
|no_network_response|没有取得有效搜索响应|否|
|request_aborted|请求被取消，必须另查取消来源，不能认定封号/断网/无帖子|否|
|browser_disconnected|页面或浏览器连接关闭；不能仅凭该错误认定人工暂停|否|
|interrupted_with_saved_sample|停止但样本保留，原因可能未知|否|
|user_paused|有明确人工暂停记录，与技术故障分开|否|
|rate_limited|429/响应头reset合法等待，不是搜索结束|否|
|authentication_hold|401/403、登录失效或验证码，保护暂停|否|
|endpoint_hold|404保护，须核验接口，非正常结束|否|
|date_query_invalid|同日起止旧date语法、日期排除或日期响应不一致，质量待核验|否|
|storage_failure|写盘失败，不能当网络问题或正常结束|否|
|guard_hold|其他保护暂停；underlying_state_code另保留底层错误|否|
|target_reached/max_pages/max_scrolls|达到人为预算，不证明搜索结束|否|
|invalid_end_evidence|声称到底但证据缺失、哈希变化、cursor链或响应不符|否|
|invalid_status_record|计数或状态字段损坏，无法安全判断|否|
|unknown_stop|未知原因默认未核验，禁止推断到底|否|

## 三个互不替代的层次

1. 任务生命周期：进程在运行、冷却、人工暂停、退出、raw.finalized，只说明执行状态。
2. 采样接受：coverage_first可接受已有样本，不等于该日期搜索到底。历史零条cursor结束记录仍保留“不自动补跑”的采样兼容规则，但严格核验计数不接纳无证据记录。
3. 搜索结束证据：只认response_end或validated_empty_tail。search_end_verified统计与采样完成统计分开。两者都不证明全平台帖子总量。

原来的apparently_exhausted字段不能单独证明到底；旧no_new_results/repeated_cursor即使保存很多帖子，也不再计为可靠结束。旧同日起止查询不能证明完整自然日。最新in_progress覆盖较早结束记录时，审计不能宣布当前访问已完成。

## 落盘与复用

保存query、时区、排序、账号/profile、最后cursor、响应文件、接收时间、SHA256及请求cursor链。正常无cursor也保存原始响应，不再仅写一个标签。search_end_status/state_label/state_category为统一解释，原stop_reason仍保留。原文件不被批量改写；新运行使用新规则，旧记录显示原始停止原因和派生待核验状态。

已完成四个半日的独立追加核验位于outputs/search_halves_20261012/{a,b}/{first_half,second_half}/search_end_verification.json。证据目录必须随研究数据保留，移动后需要重新关联并验证。未知、缺失、异常一律不能获得search_end_verified=true。

本次只修改分类及保存/识别逻辑，未自动重启采集、工作台或定时任务，未查询作者位置，未覆盖原raw/config/repair_queue。界面服务已有进程不会热更新；下次正常启动才加载新的标签与统计。92项回归通过，覆盖各停止类型、其他日期复用、正常空结果、证据缺失/篡改、游标链错误、保护覆盖、未知记录及界面计数；另用四份真实半日证据验证统一分类仍为已核验空尾部。
