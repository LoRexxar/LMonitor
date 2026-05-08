# Portal + Dashboard 页面优化设计（本地环境）

## 目标

- Portal 参考 wowdayday.com 的信息组织与审美，但数据来自本项目数据库
- Dashboard 保持后台属性与完整入口，优化信息密度与易用性
- 展示与抓取解耦：展示只读数据库；抓取任务只更新/新增数据库
- 不做前后端分离，继续使用 Django Template + 静态资源
- 不做 PortalCache 聚合 JSON，不做抓取状态表
- 抓取失败不清空历史数据，Portal 页面不出现“空白模块”

## 运行与验收环境

- 默认以已配置好的本地 settings + 真实数据库为准进行验证
- Portal 首页路由为 `/`，Dashboard 路由为 `/dashboard/`

## UI 方向

### Portal（浅色聚合页）

- 浅色卡片体系：白底/浅灰分区/边框+轻阴影，信息密度高但不拥挤
- 模块结构参考 wowdayday.com：蓝帖/热议/资讯、活动提醒、视频攻略（大标签分区）、大秘境 runs、工具导航
- 工具导航维持静态链接，内容模块全部来自数据库

### Dashboard（浅色后台）

- 入口不减少：Info 菜单展示全部 `tables_info` 表项，不做隐藏
- 表格体验：加载态/空态/错误态一致，操作按钮收敛，避免“前台资讯流”风格

## 数据模型（放在 botend app）

### 资讯（复用 WowArticle）

- 模型：`WowArticle`
- 新增字段：
  - `source`：来源标识（exwind/nga/wowhead/blizzard_tracker/blizzard_cn…）
  - `category`：类型（bluepost/hot/news/guide/datamine…）
- 唯一规则：`url` 唯一

### 活动提醒（新表）

- 模型：`PortalEvent`
- db_table：`wow_portal_event`
- 字段（最小可用集）：
  - `title`、`url`、`source`、`tag`
  - `start_at`（可空）、`end_at`（可空）
  - `status`（可空：upcoming/ongoing/ended）
  - `is_active`
- 唯一规则：`url` 唯一

### 大秘境 runs（新表，仅 runs）

- 模型：`PortalMplusRun`
- db_table：`wow_portal_mplus_run`
- 字段建议：
  - `rank`、`dungeon`、`level`、`time_seconds`、`score`（可空）
  - `tank`（可空）、`healer`（可空）
  - `dps_json`（可空）
  - `source`、`region`（可空）、`season`（可空）
  - `is_active`
- 说明：不做 affix，不做 cutoff

### 视频攻略（两表 + 一个 monitor）

- 目标表：`VideoMonitorTarget`
  - db_table：`wow_video_monitor_target`
  - 字段：
    - `name`（必填）
    - `tag`（必填，大标签）
    - `platform`（默认 bilibili）
    - `target_url`（必填，UP 主主页 URL）
    - `last_seen_bvid`（可空）
    - `is_active`
    - `ext_json`（可选）
  - 唯一规则：`platform + target_url` 唯一

- 视频表：`PortalVideo`
  - db_table：`wow_portal_video`
  - 字段：
    - `title`、`url`（唯一）、`bvid`（建议存）
    - `cover_url`（可空）、`published_at`（可空）
    - `author_name`、`author_url`（从目标表复制）
    - `tag`（从目标表复制）
    - `target`（FK → VideoMonitorTarget）
    - `is_active`
    - `extra_json`（可选）

## 抓取任务（monitors）

### 总原则

- monitor 只更新/新增数据表，不清空历史数据
- 展示只读数据表，不依赖抓取实时成功
- 任务频率默认 30 分钟（对应 MonitorTask.wait_time=1800）

### monitors 列表

- `PortalPostMonitor`：抓资讯上游源 → upsert 到 `WowArticle(source, category, url, ...)`
- `PortalEventMonitor`：抓活动上游源 → upsert 到 `PortalEvent`
- `PortalMplusRunMonitor`：抓大秘境 runs → upsert 到 `PortalMplusRun`
- `PortalVideoMonitor`：
  - 扫描所有启用的 `VideoMonitorTarget`
  - 抓 UP 主主页最新投稿，发现更新写入 `PortalVideo`
  - 更新对应 target 的 `last_seen_bvid`（增量）

## 验收标准

- Portal：资讯/活动/视频/大秘境 runs 均可展示真实数据库内容，不出现空白模块
- Portal：视频按 `tag` 分区展示，且抓取任务能持续新增内容
- Dashboard：入口不减少；表格体验更清晰；整体保持后台浅色风格
