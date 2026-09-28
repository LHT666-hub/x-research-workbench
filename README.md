# X Research Workbench

面向科研用途的本地 X（Twitter）关键词帖子采集工作台。它使用你自己的正常登录会话，在 X 网页搜索中按关键词和日期切片滚动采集帖子，并逐个补充作者在 **About this account** 中公开显示的 `Account based in`。

## 主要功能

- 关键词与日期范围检索，支持 Latest / Top。
- 按天切片、滚动到连续无新增、断点续跑。
- 可在页面中按需添加、重命名、停用和重新启用账号节点；每个节点拥有相互独立的登录会话。
- 每个日期片完成后立即处理当天新增作者。
- 帖子与作者去重、历史任务切换、覆盖诊断。
- CSV、JSON、编码模板、可视化页面和 ZIP 一键导出。
- 遇到 HTTP 429 时读取服务端等待信息并进入共享冷却，避免各节点继续冲击所在地接口。

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
