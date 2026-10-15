# 新手教程：下载到首个任务

## 1. 安装完整项目

适用 Windows。先安装 Python 3.11+ 和 Google Chrome。用 Git clone 或下载完整 ZIP 并解压到个人可写目录，不要放在 Program Files，不要只复制一个 Python 文件。

双击根目录“安装依赖.cmd”。只创建本项目 .venv 并安装 requirements.txt，不带入任何作者账号或数据。安装失败请查看脱敏错误、确认网络后重试，不删除已有研究数据。

多个 Python 可先用 py -0p 查看版本；创建环境须选 3.11+。不要混用其他项目的环境。命令行完整安装过程见根 README。

## 2. 环境自检与 Chrome

双击“检查运行环境.cmd”，或运行：

~~~powershell
.venv\Scripts\python.exe tools\x_doctor.py
.venv\Scripts\python.exe tools\x_doctor.py --json
~~~

只检查 Python、依赖、Chrome、项目文件、权限和空间，不请求 X、不读取 Cookie、不启动任务。退出码 0 表示本地必需项通过，1 表示需要修复；warning 不等于失败，更不等于已经完成采集验收。

非标准 Chrome 路径：

~~~powershell
$env:X_WORKBENCH_CHROME = 'D:\Apps\Chrome\Application\chrome.exe'
.venv\Scripts\python.exe tools\x_doctor.py
.venv\Scripts\python.exe tools\x_keyword_ui.py
~~~

临时变量在同一 PowerShell 启动才有效，不会自动传给另一个双击窗口。长期配置可在 Windows 环境变量设置中添加，再打开新终端。无效的显式路径不会回退到其他浏览器。

## 3. 打开工作台与登录

双击“打开X关键词帖子采集器.cmd”，默认 http://127.0.0.1:8766/。只供本机使用，没有远程访问鉴权。

确认没有重复工作台。端口占用时核对已有页面和服务，不终止其他 Chrome/Python。独立实例应使用独立完整项目目录与不同端口，例如 --port 8767；只换端口不会隔离同目录账号/输出，不能借此并发同账号。

在账号管理中设置自己的节点，按提示在采集专用 Chrome 中正常登录。节点名称不是内置登录账号。不要共享已被任务占用的 profile，不要把 Cookie 给陌生人。

## 4. 先做小样本

1. 选择一个账号节点和关键词，例如 gpt。
2. 选择一天；界面开始、结束均填这一天，时区按研究设计选择。
3. 使用小样本/冒烟模式和默认安全节奏。
4. 检查帖子时间、实际查询、保存文件与停止原因，再设置正式范围。

达到小样本条数/页数限制不是搜索到底。正式采集应明确范围、排序、切片、限额与接受标准。搜索返回数不是正文准确命中数。

界面日期包含结束自然日，内部查询是所选时区的半开时间戳区间。仅采 6 月 25 日时不要把界面结束日期填为 6 月 26 日，否则会包含两天。

## 5. 查看、导出与停止

普通任务保存在 outputs/x_keyword_runs/<run_id>/，保留配置、raw、status、队列与日志。页面可查看历史和导出。独立游标续采是另一类任务，原任务按钮不能控制其 worker。

TXT 为 UTF-8、制表符、一行一帖；不是检查点。不同轮次合并须按帖子 ID/URL 去重并保留来源，不修改原记录来抹掉异常。

普通任务使用其停止/暂停功能；独立 worker 按对应流程请求页边界保存，再确认进程退出。**关网页、关页面服务、关 Chrome 不能替代安全落盘验收。** 不清楚任务类型时先只读核对，不强杀。

## 6. 新用户不要直接运行什么

x_plan_cursor_continuation.py、x_guard_audit.py、x_resume_verified_five.py 等依赖既定历史任务。新安装没有这些源文件、断点和证据，不能照搬作者运行命令。HTTP 和第六账号实验也不是日常入口。

继续看 [常见问题](TROUBLESHOOTING.md)、[结束规则](../SEARCH_STATUS_RULES.md)、[安全说明](../SECURITY.md)。
