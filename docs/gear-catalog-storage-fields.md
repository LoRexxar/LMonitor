# 装备展示缓存字段边界

## 范围与调用契约

`botend.services.gear_catalog_projection.compact_catalog_content(content)` 是纯函数：返回完全脱离入参的 JSON 图，幂等，不写 DB、不请求外网、不改候选数量或排序。丢弃的大证据在遍历时直接跳过，不先 deepcopy 整份大 JSON 再删除。

必须在**中央序列化、职业专精/部位资格、属性特效、来源/轨道、美化配方资格完成后**，公共快照写文件的边界调用。不能替代 `serialize_item` / `serialize_variant`，不能套到优化器内部 ORM 对象。旧文件转换也可调用，但清理/发布/兼容存储由调用层负责。

投影在公共快照生成与存量转换边界使用，不改前端协议。以下协议字段全部原样深拷贝：物品 ID、变体 ID/key、bonus IDs、build/branch、专精已解析 `stats`、`effects`、`tooltip`、来源、制造选项、插槽、唯一装备/美化限制、`_search` / `_filter` / `_order` / `_item` 展示覆盖，以及增强目录的 `embellishments_by_equipment` 引用。

## 消费者审计

本分支没有 `static/portal/js/gear_optimizer.js`；实际辅助配装入口为 `gear_assistant.js`，结果渲染为 `gear_assistant_results.js`，后端为 `botend/services/gear_assistant.py`。审计覆盖这些实际文件，而非按不存在的文件名推测需求。

### 变体 metadata 白名单

| 字段 | 实际消费者 | 保留原因 |
|---|---|---|
| `data_branch` | `gear-catalog.js:preferredItems` | 同 ID 优先正式服、PTR 回退；历史 metadata 分支优先级不能改变 |
| `ptr_preview` | 同上 | 旧 PTR 标记兼容 |
| `venomstone` | `gear_builder.js:variantLabel` | 制造/掉落毒液石标签 |
| `primary_stat_amount` | `gear_builder.js:gemSortKey` | 主属性宝石分组排序，不是原始属性证据 |
| `two_handed` | `lockedWeaponConflict` / `validateEquipment` / `addItem` | 主副手锁定/双手武器兼容与 Fury 泰坦之握例外 |
| `item_set_id` | `wow-gear-sources.js:variantSources` | bootstrap 中套装按部位的来源投影 |
| `enchantment_id` | `gear_builder.js:simcEquipmentLine` | SimC 导出附魔 ID 后备路径 |

物品级 `metadata` 没有浏览器业务读取：`compactItem` 明确挑选顶层身份/展示字段；因此存在的 dict 投影为 `{}`（不新增本来缺失的 key，null 保持 null）。变体级 `_item.metadata` 同样处理，避免来源筛选后的名称/描述覆盖重新带回整份原始事实。

白名单之外包括 `difficulty_rule_evidence`、`simc_effect_activation_by_build`、`item_identity_by_build`、`branch_display`、`journal_builds`、localization/DB2/Raidbots 原文等，不复制到展示文件。它们仍留在原 ORM JSON，中央资格/身份/特效计算照常使用；不是删库或减少权威数据采集。`primary_stat_values/options` 等中央输入在 `stats_for_identity` 中先完成专精解析，展示仍保留该专精实际结果。

### item_identity：保留引用，去掉证据

`wow_item_identity.identity_reference` 定义的完整紧凑契约全部保留：

```
schema_version item_id game_build is_ptr inventory_type slot_key
item_class_id item_subclass_id allowable_class_mask name name_zh
primary_stat_options quality fact_hash
```

浏览器没有直接读取 `item_identity` 的字段，但不能因这一点删除中央身份引用：保留完整 reference 避免损坏兼容、构建与可追溯身份。未来/旧载荷如带 `source.evidence`、原始 fact 等附加正文，只允许以上字段通过；不把整个嵌套 identity 当作无害小对象复制。当前 captured fixture 生成的标准 reference 与投影前逐字段相等。

### Tooltip、来源与提交后消费

- `wow-item-tooltip.js:renderContent` 只消费 `name/description`；`gear_builder.js:tooltipText` 使用 canonical tooltip、装等档位、stats/effects 与来源，不读 DB2/SimC 原证据。
- `gear_assistant_results.js` 使用物品/变体顶层身份、已解析属性效果、强化、合法 acquisition sources；`gear_assistant.js` 提交锁定 equipment 时虽携带 variant 对象，后端 `_fixed_entries` 只按其 ID 重新从中央 ORM 装载，selectedStats/强化 IDs/addedSocket 另行传递，不信任展示 metadata。
- 制造入口 `resolve_crafted_variant` 根据装备/美化变体 ID 回查 DB；`embellishment_eligibility_reason` 的中央 recipe/reagent slot metadata 不能删库，但无需放进缓存。增强文件保留 options 与 equipment→option 引用，前端 `selectEnhancements`、后端 `filter_enhancement_snapshot` 继续使用同一关系。
- 分享 `compactShareState` / `WowGearState.refresh` 用 item ID + variant key、制造选择、强化引用、插槽状态；`hydrate_shared_state` 从中央恢复。SimC 导出使用顶层物品 ID/token、装等、bonus IDs、制造绿字/品质、宝石 ID 与保留的附魔 ID。
- 备选保存 `save_owned_item` 按 variant ID 回查，已知中央变体的快照在服务端重新序列化，不要求客户端带 evidence。

## 存储与转换

新发布 schema 4 每代只有 `<generation>.json.gz` 正文；`index.json` 是原子发布指针，`.meta.json` 仅保存校验与回滚描述。`bytes` 为解压后的 HTTP 正文字节，`gzip_bytes` 为磁盘压缩正文长度；`payload_sha256` 校验解压后的精确字节，`content_hash` 校验逻辑内容。

旧 schema 3 在转换前继续可读。转换只读已有文件，不查装备表、不重新采集，保留原 `source_version`；不能把旧内容冒称当前数据库最新版本。新客户端 gzip 直接流传，identity 请求流式解压，保留差异 ETag 和 Vary。

所有发布与转换共享 `gear-catalog/worker.lock`。先验证候选，再原子切换 index，失败保留旧版本。回收保留当前、最新两代非当前与最近48小时的并集。旧 JSON 只有在与保留 gzip 完整内容一致时才删除；压缩回滚仍保存原事实。未知文件、链接、损坏代际不猜测删除，无 index 和未成对的未发布文件不收集。

```sh
# 默认只读预检；生产用应用实际解释器/配置执行
python3 manage.py compact_gear_catalog_snapshots --limit 100 --lock-timeout 90
# 显式转换；按返回 last_scope 使用 --after 续跑
python3 manage.py compact_gear_catalog_snapshots --apply --limit 100 --lock-timeout 90
# 单一坐标
python3 manage.py compact_gear_catalog_snapshots --scope Warrior-Fury-head
```

按 JSON summary 的 failed、remaining、complete 对账，不以命令开始或部分转换成功称为全量完成。转换后独立核所有当前指针/正文、HTTP 解码与真实页面行为。回滚到仅支持 schema 3 的旧代码前须恢复旧描述与原 JSON 表示，不能直接把压缩归档描述设为 current。
