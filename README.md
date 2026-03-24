# LMonitor 基础架构

## 1. 项目概览

LMonitor 是一个基于 Django 的监控/采集平台，主要包含两类运行形态：

- Web 服务：提供 Dashboard 页面、认证、以及若干 API 与 Webhook 接口。
- 后台守护进程：从数据库读取监控任务，按任务类型分发到插件执行扫描/采集逻辑。

核心入口：

- Django 管理入口：[manage.py](file:///d:/program/LMonitor/manage.py)
- Django 配置：[settings.py](file:///d:/program/LMonitor/LMonitor/settings.py)
- 路由定义：[urls.py](file:///d:/program/LMonitor/LMonitor/urls.py)

## 2. 目录结构（关键部分）

- `LMonitor/`：Django project 配置层
  - [settings.py](file:///d:/program/LMonitor/LMonitor/settings.py)：数据库、静态资源、外部服务配置、线程配置
  - [urls.py](file:///d:/program/LMonitor/LMonitor/urls.py)：Web、API、Webhook 路由
  - `wsgi.py` / `asgi.py`：部署入口
- `botend/`：核心业务 App（模型、后台任务、插件、Dashboard）
  - [models.py](file:///d:/program/LMonitor/botend/models.py)：监控任务/采集结果/SimC 等数据模型
  - `controller/`：扫描插件体系（各类 monitor/scan）
  - `dashboard/`：Dashboard 页面与 API（CRUD、SimC 相关）
  - `webhook/`：对外回调入口（如 gewechat、卦象等）
  - `management/commands/`：自定义 Django 命令（后台守护进程、第三方初始化）
- `core/`：通用能力（线程池、Chrome headless、单次 LLM 请求）
  - [threadingpool.py](file:///d:/program/LMonitor/core/threadingpool.py)
  - [glm.py](file:///d:/program/LMonitor/core/glm.py)
- `utils/`：基础工具（日志、HTTP/浏览器请求封装等）
  - [LReq.py](file:///d:/program/LMonitor/utils/LReq.py)
- `templates/`、`static/`：Web 模板与静态资源
- 脚本：
  - [webstart.sh](file:///d:/program/LMonitor/webstart.sh)：启动 Web（runserver 0.0.0.0:18000）
  - [start.sh](file:///d:/program/LMonitor/start.sh)、[stop.sh](file:///d:/program/LMonitor/stop.sh)：Linux 后台守护
  - [winstart.ps1](file:///d:/program/LMonitor/winstart.ps1)：Windows 后台守护

## 3. 运行拓扑与职责边界

### 3.1 Web 服务（Django）

路由集中在 [urls.py](file:///d:/program/LMonitor/LMonitor/urls.py)：

- Dashboard 页面：`/dashboard/`（入口视图在 [dashboard.py](file:///d:/program/LMonitor/botend/dashboard/dashboard.py)）
- 认证：`/auth/login/`、`/auth/register/`、`/auth/logout/`、`/auth/change-password/`（视图在 `botend/dashboard/auth_views.py`）
- API：`/api/...`（主要在 [api.py](file:///d:/program/LMonitor/botend/dashboard/api.py)）
- Webhook：`/webhook/...`（在 `botend/webhook/`）

Web 服务以数据库为中心：Dashboard 展示与管理表数据；API 对部分业务能力提供程序化入口（例如 SimC 相关能力）。

### 3.2 后台守护进程（监控任务调度与执行）

后台入口为自定义 Django command：[LMonitorCoreBackend.py](file:///d:/program/LMonitor/botend/management/commands/LMonitorCoreBackend.py)，会调用 [LMonitorCoreBackend](file:///d:/program/LMonitor/botend/views.py#L23-L58)。

核心调度逻辑位于 [botend/views.py](file:///d:/program/LMonitor/botend/views.py)：

- `LMonitorCoreBackend`：负责创建线程池并启动多个 `LMonitorCore.scan` 线程循环。
- `LMonitorCore.scan`：
  - 使用数据库表 [MonitorTask](file:///d:/program/LMonitor/botend/models.py#L5-L13) 作为任务队列/配置源；
  - 按 `wait_time` 做最小间隔控制；
  - 根据 `type` 映射到对应插件类并执行 `scan(target)`；
  - 请求能力通过 [LReq](file:///d:/program/LMonitor/utils/LReq.py) 提供（可选 Chrome headless）。

并发控制：

- 线程池封装在 [ThreadPool](file:///d:/program/LMonitor/core/threadingpool.py)；
- 最大线程数、线程限制等参数来自 [settings.py](file:///d:/program/LMonitor/LMonitor/settings.py) 的 `THREADPOOL_MAX_THREAD_NUM`、`THREAD_LIMIT_NUM`。

## 4. 插件体系（任务类型 → 扫描实现）

任务类型的映射定义在 [config.py](file:///d:/program/LMonitor/LMonitor/config.py#L30-L48)：

- `Monitor_Type_BaseObject_List[task.type]` 返回对应的扫描类
- 插件实现位于 `botend/controller/plugins/*`

典型扩展方式：

1. 在 `botend/controller/plugins/<domain>/` 下新增扫描类（保持与既有插件一致的初始化签名与 `scan()` 行为）。
2. 在 [config.py](file:///d:/program/LMonitor/LMonitor/config.py) 中导入并追加到 `Monitor_Type_BaseObject_List`（注意顺序决定 `type` 值）。
3. 在数据库中创建/更新 `MonitorTask`，将 `type` 设置为对应索引。

## 5. 数据模型（数据库是系统中枢）

核心表集中在 [models.py](file:///d:/program/LMonitor/botend/models.py)，按用途可粗分为：

- 监控任务与认证：
  - `MonitorTask`：监控任务定义（目标、类型、扫描间隔、启用状态）
  - `TargetAuth`：目标站点认证信息（Cookie 等）
  - `MonitorWebhook`：任务相关 webhook 配置
- 采集内容存储：
  - `WechatArticle`、`RssArticle`、`WowArticle`：内容类数据
  - `VulnData`：漏洞信息存储
- SimC 相关：
  - `SimcTask`：任务执行记录（结果文件、状态等）
  - `SimcProfile`：配置档案
  - `SimcAplKeywordPair`：关键字对照
  - `SimcTemplate`：模板存储

## 6. 外部集成与能力模块

- 浏览器/采集：`utils/LReq.py` + `core/chromeheadless.py`（插件可选使用真实浏览器渲染）
- 消息/机器人接口：`botend/interface/`（如企业微信、gewechat、dify 等）
- 单次 LLM 请求封装：[core/glm.py](file:///d:/program/LMonitor/core/glm.py)（当前设计为“初始化一次、每次调用独立 messages”的单轮请求）

## 7. 运行与部署（最小集）

Web：

- `python manage.py runserver 0.0.0.0:18000`（见 [webstart.sh](file:///d:/program/LMonitor/webstart.sh)）

后台：

- `python manage.py LMonitorCoreBackend`（见 [start.sh](file:///d:/program/LMonitor/start.sh)、[winstart.ps1](file:///d:/program/LMonitor/winstart.ps1)）

数据库：

- 在 [settings.py](file:///d:/program/LMonitor/LMonitor/settings.py#L78-L92) 配置 MySQL 连接
- 使用 Django migration 管理表结构（迁移文件位于 `botend/migrations/`）

## 8. 安全与配置建议

`settings.py` 中包含多种第三方服务配置项（数据库、Webhook、外部 API、存储等）。为避免密钥泄露，建议将敏感信息迁移到环境变量或独立密钥管理方案，并在配置加载时做覆盖（例如 `os.environ`）。

