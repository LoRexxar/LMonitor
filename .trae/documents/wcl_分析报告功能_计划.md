# WCL 链接分析报告功能计划

## 1) Summary
- 目标：新增一套完整的 WCL 战斗分析能力。用户在 Web 路由输入单场 `fight` 的 WCL 链接后，系统异步抓取与解析数据，调用 `core/glm.py` 生成“职业犀利”风格的个人问题复盘，并产出可长期访问的 HTML 报告。
- 输出：可公开访问的报告页 + 可公开访问的任务列表页；任务创建后立即返回“处理中”，完成后可刷新查看结果。
- 用户已确认决策：
  - 抓取方式：页面直抓（失败时浏览器兜底）
  - 输出形态：提交后跳转结果页
  - 留存策略：HTML 文件落地永久保留 + 链接/状态入库 + 页面可查看列表
  - 语气：职业犀利（聚焦行为与机制，不做人身攻击）
  - 分析维度：通用六维
  - 权限：仅“任务结果详情链接”免鉴权；任务列表与提交入口需要鉴权
  - 链接范围：先支持单 fight（URL 必须含 `fight`）
  - 横向基准：同副本 + 同层数，抓取全球前20记录对比
  - 结果公开策略：免登录，但结果链接带随机 token 防枚举
  - Prompt方式：使用固定预设模板，先抓齐结构化数据后统一输入 GLM

## 2) Current State Analysis
- 路由集中在 [urls.py](file:///d:/program/LMonitor/LMonitor/urls.py)，当前已有 Dashboard、SimC 页面与 API 路由，新增 Web 页面/接口可沿用同一组织方式。
- 页面视图在 [dashboard.py](file:///d:/program/LMonitor/botend/dashboard/dashboard.py) 中以类视图实现；SimC 页面采用“页面路由 + API 数据接口”模式，适合复用。
- 业务 API 在 [api.py](file:///d:/program/LMonitor/botend/dashboard/api.py)；已存在 `requests`/`BeautifulSoup` 风格解析逻辑，可延续。
- 数据模型在 [models.py](file:///d:/program/LMonitor/botend/models.py)；当前无 WCL 报告任务模型，需要新增表并迁移。
- LLM 客户端已存在 [glm.py](file:///d:/program/LMonitor/core/glm.py)，可直接 `GLMClient().send_message(...)`。
- 现有模板均在 `templates/`，可新增独立页面模板，不影响现有 SimC 功能。

## 3) Proposed Changes

### A. 数据层：新增 WCL 分析任务模型
- 文件：`d:\program\LMonitor\botend\models.py`
- 新增模型：`WclAnalysisTask`
  - `wcl_url`：原始输入链接
  - `report_code`：`/reports/{code}` 里的 code
  - `fight_id`：URL query 的 fight
  - `status`：`0=待处理,1=处理中,2=成功,3=失败`
  - `error_message`：失败原因
  - `source_snapshot_file`：抓取到的原始快照文件名（JSON/TXT）
  - `report_html_file`：最终 HTML 报告文件名（落地存储）
  - `summary`：简短摘要（列表展示）
  - `created_at` / `updated_at`
  - `is_active`
- 迁移：新增 migration 文件创建表。

### B. 路由与页面：新增输入页、结果页、列表页（混合鉴权）
- 文件：`d:\program\LMonitor\LMonitor\urls.py`
- 新增路由：
  - `GET /wcl-analysis/`：输入 + 最近任务列表页（需要登录）
  - `GET /wcl-analysis/list/`：任务列表页（需要登录，可与输入页合并）
  - `GET /wcl-analysis/report/<int:task_id>/`：报告查看页（免登录）
- 文件：`d:\program\LMonitor\botend\dashboard\dashboard.py`
- 新增视图：
  - `WclAnalysisPageView`：渲染输入与列表页面
  - `WclAnalysisListView`：渲染历史任务列表（若与输入页拆分）
  - `WclAnalysisReportView`：读取落地 HTML 并渲染容器页；若任务未完成则显示状态与刷新提示
- 鉴权策略：
  - 输入页/列表页加 `login_required`
  - 报告页不加鉴权，但访问必须携带正确 token（URL 参数或 path slug）

### C. API 与异步处理：创建任务、查询状态、列表查询（鉴权）
- 文件：`d:\program\LMonitor\botend\dashboard\api.py`
- 新增 API：
  - `POST /api/wcl-analysis-task/`：提交 URL，创建任务，立即返回 `task_id` 与带 token 的结果页 URL（需要登录）
  - `GET /api/wcl-analysis-task/<int:task_id>/`：获取状态、错误、报告链接（需要登录）
  - `GET /api/wcl-analysis-task/`：分页或最近 N 条任务列表（需要登录）
- 异步执行策略（无 Celery，轻量实现）：
  - 使用 `threading.Thread(daemon=True)` 启动后台任务函数
  - 创建任务后立刻置 `status=0`，线程启动后置 `status=1`
  - 成功置 `status=2` 并写入 `report_html_file`；异常置 `status=3` 与 `error_message`
- URL 校验与规范化：
  - 仅允许 `http/https`
  - host 限定 `warcraftlogs.com`/`cn.warcraftlogs.com`
  - path 必须包含 `/reports/`
  - 必须包含 `fight` 参数（先支持单 fight）
- 任务安全字段：
  - 模型新增 `access_token`（随机 32~48 位）用于公开结果页校验
  - 结果 URL 形态：`/wcl-analysis/report/<task_id>/?token=<access_token>`

### D. 抓取与解析：页面直抓 + 浏览器兜底
- 主抓取：`requests` 拉取 HTML（带常规 UA 和超时）
- 兜底抓取：使用 [LReq.py](file:///d:/program/LMonitor/utils/LReq.py) 的 `LReq(is_chrome=True).get(..., type='RespByChrome', ...)` 获取渲染后源码
- 解析策略（按优先级）：
  1. 提取页面内可识别的结构化片段（如内嵌 JSON/script 状态块）
  2. 提取可见关键战斗文本片段（玩家、伤害/死亡/施法相关表格文本）
  3. 组装“结构化摘要对象 + 原始文本片段”供 LLM 使用
- 新增“排行榜横向对比抓取”：
  1. 从当前 fight 解析副本名与钥石层数
  2. 抓取“同副本 + 同层数”的全球前20记录页面数据（脚本抓取）
  3. 提取横向统计：顶尖队伍总耗时区间、死亡次数区间、关键技能/断法覆盖区间、常见失误类型
  4. 生成 `benchmark_summary` 结构供后续 Prompt 输入
- 失败处理：
  - 无法抓取：明确记录网络/反爬错误
  - 无法解析：记录“解析不到有效战斗数据”
  - LLM 失败：记录“模型调用失败”
  - 排行榜抓取失败：不终止主报告，标记 `benchmark_unavailable=true` 并在报告中声明“缺少横向基准”

### E. LLM 产出规范：先结构化再渲染 HTML
- 使用 [glm.py](file:///d:/program/LMonitor/core/glm.py) 的 `GLMClient.send_message`
- Prompt 约束（固定模板）：
  - 输入：战斗基础信息 + 玩家维度聚合数据/文本片段 + `benchmark_summary`
  - 模板文件：新增固定模板（例如 `core/prompts/wcl_report_prompt.txt`），使用占位符注入 JSON 数据块
  - 调用流程：先抓齐数据 -> 生成模板输入 -> 一次性发送给 GLM（必要时 JSON 纠错重试一次）
  - 输出格式：严格 JSON（便于稳定渲染），字段包含：
    - `battle_overview`
    - `key_failures`
    - `players[]`（每人六维问题、证据、优先改进建议）
    - `blame_ranking`（责任排序与理由）
    - `final_verdict`（职业犀利总结）
  - 语气限制：允许尖锐，但禁止辱骂/人身攻击/现实威胁
- 渲染方式：
  - 后端将 JSON 渲染为完整 HTML 字符串
  - 文件落地到 `static/wcl_reports/`（永久保留）
  - 数据库存储相对文件名，页面通过任务 ID 读取对应文件展示

### F. 前端页面交互
- 新增模板：`d:\program\LMonitor\templates\wcl_analysis.html`
  - 输入框（WCL URL）
  - 提交按钮
  - 提交后立即跳转 `/wcl-analysis/report/<task_id>/`
  - 下方展示最近任务列表（状态、创建时间、查看链接）
- 新增模板：`d:\program\LMonitor\templates\wcl_analysis_report.html`
  - 处理中：展示状态、错误信息、手动刷新按钮
  - 完成：内嵌渲染报告 HTML
  - 失败：展示失败原因与重试入口（回到输入页）
- 列表/输入页交互：
  - 需要登录后可访问
  - 创建任务后立即跳转公开结果页（带 token）
  - 列表展示“公开结果链接”复制按钮

### G. 与现有系统的兼容原则
- 不改动已有 SimC 业务逻辑与页面行为。
- 新功能独立路由与独立模型，避免耦合现有任务表。
- 权限边界固定为：提交/列表鉴权，报告免鉴权+token。

## 4) Assumptions & Decisions
- 决策：先不接入 WCL 官方 API 凭据链路，采用页面抓取+兜底。
- 决策：先支持“单 fight”链接；不做整 report 多 fight 拆分。
- 决策：任务异步执行，用户通过刷新查看状态，不引入消息队列/Celery。
- 决策：报告与快照文件永久保留，不做自动清理。
- 决策：横向对比基线采用“同副本+同层数”的全球前20记录。
- 决策：输入给 GLM 采用固定 Prompt 模板，禁止临时拼接随意文本。
- 决策：结果页通过 token 做不可枚举保护；仅凭 task_id 不可直接查看。
- 假设：部署环境可访问 WCL；若被反爬限制，将以失败状态返回并保留错误日志。
- 假设：`glm.py` 可正常调用并返回文本；若返回非 JSON，将做一次纠错重试（同 prompt 加“仅输出JSON”约束）。

## 5) Verification Steps
- 路由验证：
  - 未登录访问 `/wcl-analysis/` 与 `/wcl-analysis/list/` 会跳转登录
  - 提交合法 URL 后能跳转 `/wcl-analysis/report/<id>/?token=...`
  - 报告页无 token 或 token 错误返回 403/提示无权限
- 异步状态验证：
  - 新任务创建后状态从 `待处理/处理中` 变化到 `成功/失败`
  - 处理中页面刷新可看到状态更新
- 解析与报告验证：
  - 合法单 fight 链接可生成 HTML 报告并可访问
  - 报告包含六维分析、个人问题、最终总结
  - 报告包含“与同副本同层数全球前20”的横向对比结论（或明确提示基准不可用）
- 留存验证：
  - `static/wcl_reports/` 有落地文件
  - DB 中有任务记录与报告文件名映射
  - 列表页可查看历史任务并进入结果页
- 异常验证：
  - 非 WCL 链接、缺失 `fight`、抓取失败、模型失败，均有可读错误信息
- 工程验证：
  - `python manage.py makemigrations --check`（实现阶段）
  - `python manage.py check`（实现阶段）
