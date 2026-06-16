# 天赋元数据重构设计

## 目标

1. DB2 元数据区分 class/hero/spec 三棵树，用 TraitTreeID 落库
2. 保留原始坐标间距（PosX/PosY），不在元数据层压缩
3. 新增天赋文本字段（描述、副标题），用于前端展示

## DB2 数据结构

```
TraitNode.csv:
  ID, TraitTreeID, PosX, PosY, Type, Flags, TraitSubTreeID

TraitNodeEntry.csv:
  ID, TraitDefinitionID, MaxRanks, NodeEntryType, TraitSubTreeID

TraitNodeXTraitNodeEntry.csv:
  TraitNodeID → TraitNodeEntryID

TraitEdge.csv:
  LeftTraitNodeID → RightTraitNodeID（父子连线）

TraitDefinition.csv:
  ID, SpellID, OverrideIcon, OverrideName_lang, OverrideSubtext_lang, OverrideDescription_lang
```

### tree_type 判定逻辑

```
if TraitSubTreeID > 0:
    tree_type = 'hero'
else:
    # 通过 TraitTreeID 判断是 class 还是 spec
    # 需要建立 TraitTreeID → (class_name, spec_name, tree_type) 映射
    tree_type = class_or_spec_map.get(TraitTreeID, 'spec')
```

关键：**TraitTreeID 是唯一的树标识**。每个职业专精组合有 2-3 个 TraitTreeID：
- class tree（职业共享天赋）→ tree_type = 'class'
- spec tree（专精天赋，TraitSubTreeID=0）→ tree_type = 'spec'
- hero tree（英雄天赋，TraitSubTreeID>0）→ tree_type = 'hero'

### TraitTreeID → class/spec 映射方法

没有直接的 TraitTree 表。需要通过 TraitNodeEntry → TraitDefinition → SpellID 反查。
已有的 backfill 流程已经做了这个反查，只是没存 TraitTreeID。

方案：在 backfill 流程中，记录每个 TraitTreeID 对应的 (class_name, spec_name, tree_type)。
具体：遍历 TraitNode 时，通过 TraitNodeXTraitNodeEntry → TraitNodeEntry → TraitDefinition → SpellID
→ 查 WowSpellSnapshot 或 Wowhead 获取 class/spec → 建立映射表。

## 数据库改动

### WowTalentNodeMetadata 新增/修改字段

```python
# 新增
trait_tree_id = models.IntegerField(null=True, blank=True, help_text='DB2 TraitTreeID')
description = models.TextField(default='', help_text='天赋描述文本')
subtext = models.CharField(max_length=200, default='', help_text='天赋副标题（如"选择一个"）')
max_ranks = models.IntegerField(default=1, help_text='最大点数')

# 修改
# tree_type 判定逻辑改为基于 TraitTreeID + TraitSubTreeID
# column/row 保持 DB2 原始值（PosX/PosY），不再压缩
```

### 新增映射表 WowTalentTreeMap

```python
class WowTalentTreeMap(models.Model):
    """TraitTreeID → class/spec/tree_type 映射"""
    trait_tree_id = models.IntegerField(unique=True)
    class_name = models.CharField(max_length=50)
    spec_name = models.CharField(max_length=50, default='')
    tree_type = models.CharField(max_length=10)  # class/hero/spec
```

## 管线改动

### 1. backfill_talent_spell_names 改动

- `_resolve_trait_layout` 增加返回 `trait_tree_id`
- `_resolve_metadata_row` 增加返回 `description`, `subtext`, `max_ranks`
- 新增 `_resolve_trait_tree_type` 方法：通过 TraitTreeID 查映射表确定 tree_type
- bulk_update 时写入新字段

### 2. 新增管理命令 `build_talent_tree_map`

从 DB2 dump 构建 TraitTreeID → (class_name, spec_name, tree_type) 映射：
1. 遍历 TraitNode，按 TraitTreeID 分组
2. 每组取几个节点，通过 TraitNodeEntry → TraitDefinition → SpellID 反查职业专精
3. TraitSubTreeID > 0 的组标记为 hero
4. 落库到 WowTalentTreeMap

### 3. 绘图层改动（后续）

- adapters.py 不再做密集坐标压缩，直接用 DB 原始 column/row
- layout.py 用原始 PosX/PosY 计算像素坐标，保留天然间距
- 模板展示 description/subtext 文本

## 执行步骤

1. Django migration：WowTalentNodeMetadata 加字段 + 新建 WowTalentTreeMap
2. 写 `build_talent_tree_map` 命令，从 DB2 dump 构建映射
3. 改 backfill 命令，写入 trait_tree_id / description / subtext / max_ranks
4. 跑 `init_talent_metadata --refresh-tree-type --db2-dump-dir` 刷新全量数据
5. 绘图层适配（单独设计）

## 验证

- DK Frost 应有 3 种 tree_type（class/hero/spec），各有关联的 TraitTreeID
- column/row 保持 DB2 原始值（如 5400, 7200, 10800），不是压缩后的 1,2,3
- description 字段有内容的节点比例 > 50%
