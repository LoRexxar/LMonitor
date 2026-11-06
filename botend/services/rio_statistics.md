# 魔兽世界数据观察：维护说明

页面入口：`/portal/rio-statistics/`。功能包含统计总览、完成记录、大秘境热力图和团本进度，复用现有 Portal 导航、主题与中央中文资料，不提供 CSV 下载。

## 首次启用

1. 安装地图拼接依赖：`python -m pip install -r requirements-rio-statistics.txt`。
2. 按项目正常流程执行 `python manage.py migrate`，应用新增导航迁移；不批量执行其他历史数据更新命令。
3. 在业务数据库配置唯一活跃的 `SeasonMeta`，绑定有效的 `rio_season` 和正式服构建版本。正式服 `WowTalentVersion` 及中文手册发布共同参与范围标识，不按来源日期猜测当前赛季。
4. 重启 Web 和后台采集进程，加载新增插件及夜间队列。后台会按默认配置登记 `RioStatisticsMonitor`，保留既有插件编号。
5. 已有统计缓存可执行 `python manage.py check_rio_statistics --deep --migrate --backup`，再执行 `python manage.py rebuild_rio_statistics_views`。首次没有元数据和快照时由后续采集准备；页面不会在访客请求中构建数据。

## 自动更新

北京时间每日一个凌晨批次，00:00–06:00 可启动，复用原有串行长任务队列、租约和续租。白天不启动新的自动采集；已启动的长任务可继续完成。默认单轮最多 6000 次来源请求，硬上限 8000，包含元数据、重试和地图瓦片。

当天成功组合不重复采集。失败或中断最早三十分钟后在同夜续跑；429 遵守持久冷却，连续临时故障会熔断。预算不足、地图未齐、导入尚在生成、备份或展示发布失败都不能标记整轮完成。凌晨完成后数据库范围变化，可按新范围重新排队。

采集器在首个结果和后续每一百个结果处增量发布，并在轮次结束时补齐最后一批，导入命令也会维护展示文件。总览和默认 10 层以上范围优先；预算充足时先准备底图，空间数据不必等全部层数组合抓完才展示。数据库赛季或版本变化后接口立即停止使用旧范围；新范围未准备好时显示等待。已打开页面每分钟读取小状态，发现修订变化后更新当前结果并保留筛选。

## 存储与发布

默认数据根目录为 `var/rio-statistics/`，可由项目设置 `RIO_STATISTICS_ROOT` 指定。采集和 Web 进程必须访问同一数据根目录。本实现适用于同机多进程；不要直接将 SQLite 缓存库作为网络共享盘数据库使用。

- `snapshots.sqlite3`：独立缓存库，正文压缩并校验，正文、索引和轻量日历史同事务提交；业务赛季和任务仍在现有主数据库。
- 地图 WebP、瓦片断点和元数据目录：保留文件形式，地图严格匹配挑战模式编号、楼层和有向坐标边界。
- `views/current.json`：唯一原子发布指针，也是小状态来源。
- `views/catalog-a.json`、`catalog-b.json`：后台使用的全量双槽工作清单，公开请求不再打开它们。
- `views/reader-a.json`、`reader-b.json`：前端初始化与筛选校验用的轻量目录，提供代表范围及完整数量；全部副本、层数和高级筛选仍保留。
- `views/indexes/<哈希前缀>-a.json`、`<哈希前缀>-b.json`：按键前两个十六进制字符分为最多 256 个索引分片，单项请求只定位自己的分片。
- `views/entries/<筛选摘要>-a.json`、`<筛选摘要>-b.json`：每组合双槽展示正文。

变化内容先写非活动槽并校验，最后原子切换指针。提交前失败保留上一完整发布；提交后中断时新发布已经完整。读者核对范围及正文标记，不读取未提交候选文件。旧 JSON 布局无删除迁移，首次升级保留旧展示文件。

同范围更新失败保留已有正文和真实采集时间；源缓存损坏时可继续保留已验证的同范围展示，标为旧数据并记录未完成。赛季、版本或周编号变化不跨范围借用旧数据。超过三十六小时的展示标为旧数据。

公开 GET 只读轻量目录、对应索引分片及正文，不枚举统计库、不解压统计正文、不查询中文词条，也不解析全部历史组合的工作清单。仍检查业务数据库范围。API 每次校验版本并支持 ETag、304 和 gzip；地图为内容摘要地址，可长期缓存。不同缓存键的冷构建不占用同一把全局构建锁；单键仍合并重复构建。展示文件缺失或损坏时不在请求中回源或重建。

自动发布只处理本批改变的键并复用已提交正文；元数据的每日更新时间不再触发全部历史正文重建。管理员默认重建仍逐份校验，可用于修复损坏；旧布局快速升级使用下述复用选项。布局版本采用兼容字段，旧读取字段保留，但应更新并重启全部 Web 和后台采集进程，避免旧进程重新写回旧布局。

夜间执行深度检查和七个固定星期槽备份；来源限流或元数据失败时仍尝试本地维护。备份位于同一数据根目录，不能代替异机备份。默认全目录上限 10 GiB、磁盘最低可用 256 MiB，分别可由 `RIO_STATISTICS_MAX_STORAGE_BYTES`、`RIO_STATISTICS_MIN_FREE_BYTES` 配置。包括地图、旧文件、中间文件和备份，空间不足停止增长并保留有效数据，不自动删除文件。

## 命令

```powershell
# 当前范围总览试采
python manage.py sync_rio_statistics --modules overview --regions cn world --max-requests 8

# 指定热力图范围；地图首次准备也计入预算
python manage.py sync_rio_statistics --modules heatmaps --regions cn --dungeons den-of-nalorakk --levels level-12 --periods season --max-requests 100

# 从已有统计库重建展示；不请求外网
python manage.py rebuild_rio_statistics_views

# 大目录快速升级：复用已提交且未改变的正文，不进行全量正文复核
python manage.py rebuild_rio_statistics_views --reuse-published

# 先查看最近批次卡在哪个阶段；不扫描全部正文或目录
python manage.py check_rio_statistics --progress-only

# 检查结构、正文、历史、展示、地图、容量和最近夜间批次
python manage.py check_rio_statistics --deep

# 首次迁移旧缓存并生成备份，保留原文件
python manage.py check_rio_statistics --deep --migrate --backup

# 导入已有授权来源的完成记录聚合响应
python manage.py import_rio_run_statistics C:/data/rio-runs.json
```

采集默认对未完成、冷却或忙碌状态返回非零退出码；主动进行部分试采可加 `--allow-partial`。巡检默认警告和错误均非零退出；明确允许已知警告时可加 `--allow-warning`，错误仍不放行。展示保留旧数据但来源尚未修复时，重建命令也报告未完成。

## 数据能力与限制

热力图支持有限汇总、层数区间、2–30 单层及 2–99 自选层数，提供周区间、职业、专精、死亡者身份、已收录词缀、装等、用时和死亡数筛选。高级组合通过显式 CSRF 登记，由凌晨任务处理；不在访客 GET 中登记或获取来源数据，也不遍历所有维度的笛卡尔积。

完成记录目前只支持严格导入和夜间导入目录维护，没有配置自动获取新统计的上游来源，不自动抓取来源未公开的统计端点。默认导入目录为数据根目录 `imports/runs/`，可由项目设置 `RIO_STATISTICS_RUNS_SOURCE_DIR` 指定；夜间最多检查最新五十份 JSON 响应。巡检会明确提示缺少自动来源。

队伍组成、首领尝试等原站维度尚未实现，不宣称完整覆盖。角色数不等于现实独立玩家数，死亡热度不等于死亡概率，聚合网格不能还原逐把移动路线。空缺数据保持空缺。

## 回归

```powershell
python manage.py test botend.tests.test_rio_statistics_scale botend.tests.test_rio_statistics_publication botend.tests.test_rio_statistics_views botend.tests.test_rio_statistics botend.tests.test_rio_statistics_dimensions botend.tests.test_rio_statistics_maps_schedule botend.tests.test_rio_statistics_database_scope botend.tests.test_monitor_night_lane botend.tests.test_rio_statistics_maintenance --noinput
python manage.py check
python manage.py makemigrations --check --dry-run
node --check static/portal/js/rio-statistics.js
```

当前本地 SQLite 回归共 123 项，122 项通过，一项真实 MySQL 行锁测试跳过。已验证真实缓存无损迁移、跨日自动更新、范围切换、发布失败、进程强退、提交后中断、慢读槽复用、损坏恢复，以及增量发布和大目录读取隔离。独立验收页面及真实地图通过桌面和手机验证。

早期 69 条真实样本和 4864 组合的压力结果不足以支持“线上没有读取压力”。2026 年 11 月 6 日线上目录实测已有 47724 条，解压约 14 MB；复刻工作清单对象超过 64 MiB 缓存预算，会反复解析并争用全局锁。此次按真实坐标复刻目录结构，验证单项请求不读全量清单；只复刻真实总览和一份热力图正文，不代表完成线上全部来源初始化。生产完整夜间运行、初始化总耗时和 MySQL 并发仍需以运行记录验证；本地测试或推送本身不能代替该验收。
