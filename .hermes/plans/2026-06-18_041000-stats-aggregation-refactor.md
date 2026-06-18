# 2026-06-18 团本/M+/玩家详情 — 数据提取 + 聚合重构

## 目标
1. 从现有 gear_json / talents_json / stats_json 提取隐藏维度（宝石、属性、种族等）
2. 把 on-demand 实时聚合改为 monitor 预计算 + 读 JSON 文件
3. 玩家详情页和概览页都展示提取出的数据

## 现有数据盘点

| 数据 | 来源 | 覆盖率 | 用途 |
|------|------|--------|------|
| race | PlayerSpecTopPlayer.race | 100% | 种族分布 |
| faction | SpecRaidRanking.faction | 100% | 阵营分布 |
| guild | PlayerSpecTopPlayer.guild_name | 93% | 公会分布 |
| stats_json (crit/haste/mastery/vers) | PlayerSpecTopPlayer.stats_json | 30% | 属性分布 |
| gear_json.gems | SpecRaidRanking.gear_json | ~60%有宝石 | 宝石选择率 |
| gear_json.itemLevel | SpecRaidRanking.gear_json | 100% | 装等分布 |
| gear_json.icon | SpecRaidRanking.gear_json | 100% | 装备图标展示 |
| gear_json.name | SpecRaidRanking.gear_json | 100% | 装备名称 |
| gear_json.bonusIDs | SpecRaidRanking.gear_json | 100% | 附魔推断（间接） |
| talents_json | SpecRaidRanking.talents_json | 100% | 天赋节点统计 |
| talent_build_code | SpecRaidRanking.talent_build_code | ~5% | 天赋导入码 |

**注意**：WCL 的 gear_json 中 slot 字段全部是 "unknown"，无法直接知道哪个装备在哪个槽位。附魔数据不直接可用（bonusIDs 是加密的数值 ID，需要 item-sparse.db2 才能映射）。

## 架构

```
Monitor 定时脚本 aggregate_spec_stats
  ↓ 读 SpecRaidRanking + PlayerSpecTopPlayer
  ↓ 按 season/class/spec/boss(dungeon) 分组聚合
  ↓
JSON 文件: media/aggregated/{season_id}/{type}/{class}_{spec}/{enc_id}.json
  ↓
View 读 JSON 文件
  ↓
Template 渲染
```

## 聚合维度

### 概览页 / 详情页共享的 JSON 结构

```json
{
  "meta": {
    "season_id": 2, "class_name": "Warrior", "spec_name": "Arms",
    "encounter_id": 3176, "encounter_name": "Imperator Averzian",
    "sample_size": 100, "updated_at": "2026-06-18T00:00:00"
  },
  "dps": {
    "avg": 115388, "median": 112000, "p25": 95000, "p75": 130000,
    "max": 142781, "min": 80000
  },
  "kill_time": { "avg": 249000, "median": 240000 },
  "talents": {
    "builds": [
      {"build_code": "...", "count": 15, "avg_dps": 120000, "pct": 15.0}
    ],
    "tree_render": { ... }  
  },
  "gear": {
    "avg_item_level": 289.5,
    "gems": [
      {"gem_id": "240890", "count": 45, "pct": 45.0, "avg_dps": 120000},
      {"gem_id": "240906", "count": 30, "pct": 30.0, "avg_dps": 118000}
    ],
    "enchant_hints": [
      {"bonus_id": "13335", "count": 80, "pct": 80.0}
    ]
  },
  "stats": {
    "crit": {"avg": 35.2, "median": 34.5, "p25": 30.0, "p75": 40.0},
    "haste": {...}, "mastery": {...}, "versatility": {...}
  },
  "race": {"Orc": 25, "Human": 20, "Tauren": 15},
  "faction": {"Horde": 55, "Alliance": 45},
  "guilds": [{"name": "Method", "count": 5}, ...]
}
```

### 玩家详情页

保持现有结构，新增展示：
- 种族（race）
- 阵营（faction）
- 公会（guild）
- 宝石列表（从 gear_json.gems 提取）
- 装等（从 gear_json.itemLevel 计算平均）
- 属性（从 stats_json，已有但可能为空）

**不做对比**，只展示玩家自己的数据。

## 实现步骤

### Phase 1: 聚合脚本
**新建**: `botend/management/commands/aggregate_spec_stats.py`

1. 读取 active season
2. 遍历所有 class/spec × encounter 组合
3. 从 DB 查询该组合的全部记录
4. 计算 DPS/击杀时间统计
5. 按 talent_build_code 分组统计天赋选择率
6. 从 gear_json 提取宝石统计
7. 从 PlayerSpecTopPlayer 提取种族/公会/属性统计
8. 写入 JSON 文件

### Phase 2: Service 层
**重写**: `botend/services/spec_stats_service.py`

概览页方法改为读 JSON 文件：
- `get_raid_overview()` → 读所有 boss 的 JSON
- `get_dungeon_overview()` → 读所有 dungeon 的 JSON
- `get_raid_detail()` → 读单个 boss 的 JSON
- `get_dungeon_detail()` → 读单个 dungeon 的 JSON

### Phase 3: 模板
**重写**: `raid_stats.html`, `dungeon_stats.html`
- 天赋展示：build code 选择率列表
- 装备展示：宝石选择率 + 装等分布
- 新增：种族分布、阵营分布
- 新增：属性区间

**小改**: `player_detail.html`
- 新增种族、阵营、公会展示
- 新增宝石列表
- 新增装等

### Phase 4: Monitor 调度
`LMonitor/config.py` 注册新 monitor task

## 需要修改的文件
1. `botend/management/commands/aggregate_spec_stats.py` — **新建**
2. `botend/services/spec_stats_service.py` — **重写**
3. `botend/portal/spec_detail_views.py` — 小改
4. `botend/templates/portal/spec_detail/raid_stats.html` — **重写**
5. `botend/templates/portal/spec_detail/dungeon_stats.html` — **重写**
6. `botend/templates/portal/spec_detail/player_detail.html` — 小改
7. `LMonitor/config.py` — 注册 monitor

## 验证
1. `python manage.py aggregate_spec_stats` 检查 JSON 生成
2. 访问团本/M+ 概览页验证
3. 访问玩家详情页验证新增数据
4. 性能对比：新旧加载时间
