# 研究数据在哪里

以下路径相对于**本机运行主目录project/**。GitHub发布目录自身没有这些研究数据。原始文件和历史搜索记录不删除、不通过改标签冒充完整。

|层次|路径|读取含义|
|---|---|---|
|原五批数据|outputs/x_keyword_runs/<run_id>/raw.json|原始posts、authors和search_runs；原配置run_config.json、状态status.json、修复repair_queue.json同目录|
|10月8日独立补采|outputs/staged_collection_20261008/{a,b,c,e}/collection/supplement_raw.json|这一轮新增数据，不替代原批次|
|D的29日期独立补采|outputs/quality_iteration_20261008/d_supplement/supplement_raw.json|D独立新增数据；不是整个D原始结果|
|10月12日游标续采|outputs/cursor_continuation_20261012/{a,b,c,d,e}/collection/continuation_raw.json|从历史检查点增加的数据；同目录status.json、日志、search_end_evidence、404_verification记录证据|
|原五批TXT快照|outputs/exports_txt_20261008/*_posts.txt|10月8日导出快照，不包含后续所有增量|
|续采新增TXT|outputs/cursor_continuation_20261012/<node>/collection/<BATCH>_continuation_new_posts.txt|只有续采新增；需与原数据及前轮补采按ID/URL去重合并|
|正文命中/待复核|outputs/quality_iteration_20261008/|body_hit.txt、body_not_hit_review.txt、每日质量表；正文未命中不直接等于无关|
|结束对照实验|outputs/search_halves_20261012/、search_bottom_20261012*/、terminal_cursor_probe_20261012*/|实验样本和证据，不等于全研究最终数据|
|登录状态|tmp/chrome-x-keyword-profile*|Cookie及账号浏览器资料；只在本机，不能上传或共享|

## 五批对应关系

|账号/批次|关键词|原run_id|
|---|---|---|
|A|gpt（部分旧查询保存为GPT）|20260928_122030|
|B|claude|20260928_233433_b|
|C|gemini|20260929_001443_c|
|D|deepseek|20260929_002436_d|
|E|kimi|20260929_105257_e|

**没有一个现有TXT可以直接称为“截至当前所有数据已合并的最终版”。** 原五批、前轮补采、10月12日续采分别保存，应在稳定快照上按帖子ID/URL去重后生成新合并导出，并保留来源清单。

TXT为UTF-8 BOM、制表符分栏，一条帖子一行；正文换行转义为`\n`。用Excel导入时选UTF-8和制表符，长帖子ID按文本处理。JSON是原始结构化主数据，TXT是阅读/分析导出，不是恢复检查点。

2026-10-15新增可选存储：只有显式以`--checkpoint-backend sqlite`启动独立续采时，`continuation_raw.sqlite3`才是该续采的权威检查点，原JSON变成可能滞后的兼容快照；`status.json`会明确标注。现有五批没有执行转换。用`tools/x_read_checkpoint.py <continuation_raw.json>`读取权威状态；旧直接读取JSON的统计脚本不能当作SQLite模式的实时台账。WAL备份、哈希锚点及保护规则见[模块与存储说明](MODULAR_ARCHITECTURE.md)。

移动证据文件会改变其绝对路径；结束判定需要重新关联并校验SHA256。现在不搬迁原始数据或浏览器目录，避免破坏断点和证据关联。
