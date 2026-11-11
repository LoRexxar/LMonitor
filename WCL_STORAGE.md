# WCL CombatantInfo 存储与迁移

## 存储职责

- `wcl_combatant_snapshot`：采集与身份/完整性校验入口；调用中央事实 Service，不再把新 CombatantInfo 原文写入旧表。
- `wcl_raw_staging`：原文 gzip 短期暂存，供数据库失败恢复；不是第二个长期事实源，不是业务事实持久化的成功前提。
- `wcl_combatant_store`：唯一规范事实读写入口。`WclCombatantFact` 保存 report/fight/actor、必要身份/属性、原采集时间和内容引用；`WclCombatantDocument` 按 kind+内容 SHA 复用完整必要装备/天赋数组。
- 旧 `WclCombatantSnapshot` 暂保留为只读兼容来源。读取合并新旧事实，同键 compact 优先；整场合法角色保留，不仅保存上榜者。

原文中的未消费字段不进入新业务事实。原始 hash 是校验摘要，不是原文备份，也不能据此反推完整响应。装备、天赋、属性、种族、身份及 `fetched_at` 的业务语义须保持。

## 私有暂存

| 设置 | 默认值 |
|---|---|
| `WCL_RAW_STAGING_DIR` | `BASE_DIR / var / wcl-raw-staging` |
| `WCL_RAW_STAGING_TTL_SECONDS` | 172800（48 小时） |
| `WCL_RAW_STAGING_MAX_BYTES` | 536870912（512 MiB，载荷预算） |
| `WCL_RAW_STAGING_MAX_FILE_BYTES` | 16777216（16 MiB，同时约束压缩文件及解压后载荷） |

另限制最多 4096 个载荷文件。目录须为专用私有目录，拒绝 media、路径穿越、未知成员及符号/硬链接。读取/写入时执行轻量过期与容量维护，实际命中再解压校验；恢复不续原 TTL。成功提交后回收对应暂存。长期停采时不承诺恰在 TTL 时钟到点主动唤醒删除；已有内容仍受容量上限约束，运维可显式调用 `prune_staging()`。

暂存路径/配置/解码/清理故障仅记录不含身份的状态，不能让有效 HTTP 结果丢失或把已提交事实报告为失败。真正的数据库写入错误仍须失败并保留恢复机会。

采集预检、写入去重和写后身份核对使用轻量身份投影，不加载共享装备、天赋文档；旧表兼容读取由数据库提取身份字段。实际统计聚合仍读取完整规范事实。两种读取都保留迁移退役期间的补读保护。

恢复暂存时按每场战斗传递原始采集时间，批量入库不以恢复时间替代。完整空响应产生的缺失状态也沿用原始时间计算重试期限，避免恢复旧响应后重新延长负缓存。普通网络请求以响应完成时间记时。

人物本地回填缓存按候选人物的当前赛季、专精榜单提取战斗引用，只检查相关报告、战斗的事实行数和最大主键。无关战斗新增事实不会清空其他人物的命中或缺失结果；相关战斗新增事实则立即重查依赖它的人物，同场人物共享这一失效范围。旧表和新表分别计数，兼容较小主键延迟提交及迁移退役。版本检查不读取事实 JSON；缓存命中不延长原有 24 小时复核期限，榜单版本变化仍按专精失效。

暂存写入在锁外完成事件序列化、摘要和压缩，事件列表只编码一次。预处理按最多 20 个文件、16 MiB 压缩缓冲分组；单文件仍受配置的大小上限约束，分组切换时最多额外保留一个已准备的文件。取得锁后重新检查过期时间、容量和同内容文件，保留原子替换、文件与目录同步，恢复重放不续期。容量维护仍在每组写入前执行，默认一次 20 场的请求通常只维护一次。

采集进度日志新增 `validation_seconds`、`staging_seconds`、`fact_transaction_seconds`、`fact_lookup_seconds`、`fact_write_seconds`、`fact_readback_seconds`、`fetch_state_seconds`、`staging_cleanup_seconds`。事实事务总耗时包含查重、写入及提交或回滚；查重和写入另行细分。暂存内部另有 `staging_prepare_seconds`、`staging_lock_wait_seconds`、`staging_lock_hold_seconds`、`staging_write_seconds`，均累计异常路径的耗时。内部指标包含在暂存总耗时内，写入耗时又包含在持锁耗时内，不能直接全部相加；暂存命中读取仍计入请求阶段，清理耗时在提交回调实际执行时累计。

## 存量迁移：默认不写、不删

0274 只添加两张表，不扫描迁移旧原文。以下命令仅在新版本部署后、获准的生产维护环境执行：

```bash
# 有界预览，不写业务数据库
.venv/bin/python manage.py migrate_wcl_combatant_facts --after-id 0 --limit 20

# 明确授权复制后才执行；不删除旧行
.venv/bin/python manage.py migrate_wcl_combatant_facts --after-id 0 --limit 20 --apply
```

默认最多 500 行，硬上限 2000 行；默认 8 MiB 保守载荷预算。行数是上限，不承诺一批一定处理到该数量；预算选择可容纳的连续 PK 前缀。单行也放不下时明确拒绝，不能靠抬 MySQL packet 或超时放行。

每次保留 JSON 回执，按成功回执 `end_pk` 续跑；检查 `remaining`，不要只看退出码。复制后逐行从新 Fact/Document 直接回读核验，不用 legacy fallback 掩盖缺失。原微秒采集时间保留；重复执行不覆盖已存在事实。

只有取得单独退役授权、核实数据库机磁盘/binlog 余量和备份/恢复边界后，才可额外使用 `--retire-legacy`，并且必须同时 `--apply`。退役在事务中精确锁定源 PK，再核身份、原文摘要、业务投影和采集时钟；不是按年龄清表。不要将复制命令当作全库清理授权。

## 并发、生命周期与回滚

- 兼容读取先读 compact、再读 legacy，结束后补读尚未见过的精确 compact PK，覆盖 copy+retire 在两个 legacy 查询之间提交的窗口。不能仅加 `atomic` 宣称 READ COMMITTED 下有跨语句一致快照，也不能用 max-PK 水位过滤补读。
- 新业务 Fact 本身尚无按年龄自动淘汰策略；引用闭包未证明的正事实不能作为缓存猜删。Document 受 FK 保护，不能按相同 ItemID/TalentID 而忽略内容差异进行合并。
- 新写后不能直接退回只认识旧表的代码；优先前向修复，必要回滚必须保留新读取兼容层或制定明确的业务事实镜像方案。0274 反向迁移在 Fact/Document 非空时拒绝 DROP，不能以 fake migration 或手删表绕过。
- 逻辑 JSON 体积、InnoDB 分配空间、旧行删除、binlog 增量、物理磁盘归还分别核验。ROW/FULL 的 DELETE 可能产生完整 before-image；删除行不等于 `.ibd` 当场缩小。禁止直接删 MySQL 数据/undo/redo/binlog 文件，不自动 OPTIMIZE、TRUNCATE 或重建旧表。

## 验证入口

```bash
flock -w 180 /tmp/lmonitor-test-suite.lock .venv/bin/python -B manage.py test \
  botend.tests.test_wcl_combatant_store \
  botend.tests.test_wcl_combatant_migration \
  botend.tests.test_wcl_combatant_retire_race \
  botend.tests.test_wcl_raw_staging \
  --settings=LMonitor.settings_test_sqlite --noinput
```

SQLite 正规回归不代替实际 InnoDB 迁移、并发、时钟精度和约束验证。部署后还须读回真实 schema/ledger、新事实与旧行保护，再检查实际人物/统计页面。旧样本私有重放、新写入链路上线、自然日更完成、全量旧表退役应分别报告。
