# WoW Spec Detail Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 WoW 专精详情的数据错误、本地验证链路和 Portal 天赋展示，使榜单、数据结构和页面都能在本地验证后可靠发布。

**Architecture:** 先修正人物榜抓取逻辑并增加本地验证命令，再收敛 Windows 开发配置和 monitor 执行入口，随后统一天赋数据结构并扩展服务层聚合，最后重做 Portal 专精详情模板与样式。所有改动按独立问题面切分提交，并在每次提交前完成本地验证。

**Tech Stack:** Django, MySQL, Python, Django templates, CSS, Raider.IO API, WCL API, Battle.net API

---

## 文件责任映射

- `botend/controller/plugins/portal/SpecDetailBase.py`
  - Raider.IO 请求封装，修正分页默认值和世界榜请求行为。
- `botend/controller/plugins/portal/SpecDetailPlayerMonitor.py`
  - 修正 Top20 抓取策略，优先使用 `world` 榜单，统一 rank 写入语义。
- `botend/services/spec_stats_service.py`
  - 修正人物榜读取和天赋聚合输出。
- `botend/portal/spec_detail_views.py`
  - 为模板提供更完整的展示上下文。
- `botend/templates/portal/spec_detail/player_list.html`
  - 重做人物榜页面结构。
- `botend/templates/portal/spec_detail/player_detail.html`
  - 重做玩家详情页，重点替换天赋展示。
- `botend/templates/portal/spec_detail/dungeon_stats.html`
  - 接入统一天赋热力图组件。
- `botend/templates/portal/spec_detail/raid_stats.html`
  - 接入统一天赋热力图组件。
- `botend/constants/wow.py`
  - 扩展天赋树和展示常量。
- `LMonitor/settings_dev.py`
  - 修正 Windows 本地数据库连接兼容性。
- `botend/management/commands/verify_spec_detail.py`
  - 新增本地验证命令。

## Task 1: 修正 Raider.IO 人物榜抓取

**Files:**
- Modify: `botend/controller/plugins/portal/SpecDetailBase.py`
- Modify: `botend/controller/plugins/portal/SpecDetailPlayerMonitor.py`

- [ ] **Step 1: 编写失败验证脚本，确认当前抓取的是第 21-40 名**

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor; from botend.models import SeasonMeta; m=SpecDetailPlayerMonitor(None,None); s=SeasonMeta.objects.filter(is_active=True).first(); print([(i+1,p.get('name'),p.get('region'),p.get('score')) for i,p in enumerate(m._fetch_top_players('Monk','Windwalker', s.rio_season)[:5])])"
```

Expected: 输出 `Zehe / Isnoob / Kripton` 一类角色，而不是世界榜真实前五。

- [ ] **Step 2: 修正 `fetch_raiderio_top()` 默认分页**

```python
def fetch_raiderio_top(self, class_name, spec_name, season, region='us', limit=20, page=0):
    ...
```

- [ ] **Step 3: 让 `_fetch_top_players()` 优先使用世界榜**

```python
def _fetch_top_players(self, class_name, spec_name, season):
    data = self.fetch_raiderio_top(class_name, spec_name, season, region='world', limit=20, page=0)
    rankings = data.get('rankings', {}) or {}
    ranked_characters = rankings.get('rankedCharacters', []) if isinstance(rankings, dict) else []

    players = []
    for r in ranked_characters[:20]:
        ...
        players.append(player)

    if len(players) < 20:
        players = self._fetch_top_players_from_regions(class_name, spec_name, season)

    self._enrich_talents(players)
    return players[:20]
```

- [ ] **Step 4: 提取地区兜底函数，保留旧策略但修正分页**

```python
def _fetch_top_players_from_regions(self, class_name, spec_name, season):
    all_players = []
    for region in self.REGIONS:
        data = self.fetch_raiderio_top(class_name, spec_name, season, region=region, limit=20, page=0)
        ...
    all_players.sort(key=lambda p: p.get('score', 0) or 0, reverse=True)
    return all_players[:20]
```

- [ ] **Step 5: 修正落库时 rank 的写入语义**

```python
for i, player in enumerate(players):
    PlayerSpecTopPlayer.objects.create(
        ...
        rank=i + 1,
        ...
    )
```

Expected: 数据库里同一专精只会出现 `1..20`，不会出现地区内重复 rank。

- [ ] **Step 6: 运行本地抓取函数验证**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor; from botend.models import SeasonMeta; m=SpecDetailPlayerMonitor(None,None); s=SeasonMeta.objects.filter(is_active=True).first(); print([(i+1,p.get('name'),p.get('region'),p.get('score')) for i,p in enumerate(m._fetch_top_players('Monk','Windwalker', s.rio_season)[:20])])"
```

Expected: 前几名应包含 `Dabnel / Эльпочита / 龍丨大師 / Cixxmonk / Темканайк`。

- [ ] **Step 7: 提交**

```powershell
git add 'botend/controller/plugins/portal/SpecDetailBase.py' 'botend/controller/plugins/portal/SpecDetailPlayerMonitor.py'
git commit -m 'fix(spec-detail): correct raiderio top player paging'
```

## Task 2: 建立本地验证命令

**Files:**
- Create: `botend/management/commands/verify_spec_detail.py`

- [ ] **Step 1: 新建验证命令骨架**

```python
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '验证 WoW 专精详情数据链路'

    def add_arguments(self, parser):
        parser.add_argument('--class-name', required=True)
        parser.add_argument('--spec-name', required=True)

    def handle(self, *args, **options):
        self.stdout.write('verify start')
```

- [ ] **Step 2: 加入数据库榜单读取与实时榜单拉取**

```python
from botend.models import SeasonMeta, PlayerSpecTopPlayer
from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase

season = SeasonMeta.objects.filter(is_active=True).first()
db_rows = list(PlayerSpecTopPlayer.objects.filter(
    season_id=season.id,
    class_name=class_name,
    spec_name=spec_name
).order_by('rank').values_list('rank', 'character_name', 'region', 'score')[:20])

base = SpecDetailBase(None, None)
rio_data = base.fetch_raiderio_top(class_name, spec_name, season.rio_season, region='world', limit=20, page=0)
```

- [ ] **Step 3: 输出逐项差异并在不一致时返回非零退出码**

```python
import sys

mismatches = []
...
if mismatches:
    self.stderr.write(str(mismatches))
    sys.exit(1)
self.stdout.write('verify ok')
```

- [ ] **Step 4: 运行验证命令**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py verify_spec_detail --class-name Monk --spec-name Windwalker
```

Expected: 若数据库未刷新则 FAIL；刷新后 PASS。

- [ ] **Step 5: 提交**

```powershell
git add 'botend/management/commands/verify_spec_detail.py'
git commit -m 'feat(spec-detail): add top player verification command'
```

## Task 3: 修正 Windows 开发配置

**Files:**
- Modify: `LMonitor/settings_dev.py`

- [ ] **Step 1: 查看当前数据库 `OPTIONS` 中的 `init_command`**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from django.conf import settings; print(settings.DATABASES['default'].get('OPTIONS'))" --settings=LMonitor.settings_dev
```

Expected: 看到当前多语句 `init_command` 配置。

- [ ] **Step 2: 将不兼容的多语句改为单语句，必要时拆分到其他配置项**

```python
'OPTIONS': {
    'charset': 'utf8mb4',
    'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
}
```

- [ ] **Step 3: 运行 Django 检查**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py check --settings=LMonitor.settings_dev
```

Expected: `System check identified no issues`。

- [ ] **Step 4: 运行数据库连通验证**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute('SELECT DATABASE(), VERSION(), 1'); print(c.fetchone())" --settings=LMonitor.settings_dev
```

Expected: 输出库名、版本和 `1`。

- [ ] **Step 5: 提交**

```powershell
git add 'LMonitor/settings_dev.py'
git commit -m 'fix(dev): make windows mysql settings compatible'
```

## Task 4: 用 monitor 刷新并校验人物榜

**Files:**
- Modify: `botend/controller/plugins/portal/SpecDetailPlayerMonitor.py`
- Test: `botend/management/commands/verify_spec_detail.py`

- [ ] **Step 1: 手动执行人物榜 monitor**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor; m=SpecDetailPlayerMonitor(None, type('T', (), {'flag':'', 'save':lambda self:None})()); print(m.scan(''))"
```

Expected: 返回 `True`。

- [ ] **Step 2: 核对数据库中踏风榜单**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py shell -c "from botend.models import PlayerSpecTopPlayer, SeasonMeta; s=SeasonMeta.objects.filter(is_active=True).first(); qs=PlayerSpecTopPlayer.objects.filter(season_id=s.id, class_name='Monk', spec_name='Windwalker').order_by('rank'); print([(x.rank,x.character_name,x.region,float(x.score)) for x in qs[:20]])"
```

Expected: 数据与世界榜前 20 对齐。

- [ ] **Step 3: 运行验证命令确认 PASS**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py verify_spec_detail --class-name Monk --spec-name Windwalker
```

Expected: PASS。

- [ ] **Step 4: 提交**

```powershell
git add 'botend/controller/plugins/portal/SpecDetailPlayerMonitor.py' 'botend/management/commands/verify_spec_detail.py'
git commit -m 'fix(spec-detail): align stored top players with world rankings'
```

## Task 5: 统一天赋数据结构

**Files:**
- Modify: `botend/controller/plugins/portal/SpecDetailBase.py`
- Modify: `botend/controller/plugins/portal/SpecDetailPlayerMonitor.py`
- Modify: `botend/services/spec_stats_service.py`
- Modify: `botend/constants/wow.py`

- [ ] **Step 1: 增加统一节点标准化函数**

```python
def normalize_talent_node(node, tree_type='spec'):
    return {
        'tree_type': tree_type,
        'talent_id': node.get('talentID') or node.get('talent_id'),
        'spell_id': node.get('spellID') or node.get('spell_id') or node.get('talentID'),
        'name': node.get('name', ''),
        'icon': node.get('icon', ''),
        'points': node.get('points', 0) or 0,
        'row': node.get('row'),
        'column': node.get('column'),
    }
```

- [ ] **Step 2: 统一 WCL 解析结果**

```python
def parse_wcl_talents(talent_list):
    if not talent_list:
        return []
    return [normalize_talent_node(t, tree_type=t.get('tree', 'spec')) for t in talent_list]
```

- [ ] **Step 3: 统一 Raider.IO profile 解析结果**

```python
result.append(normalize_talent_node({
    'talentID': talent.get('id'),
    'spellID': spell.get('id'),
    'name': spell.get('name', ''),
    'icon': spell.get('icon', ''),
    'points': 1,
    'row': talent.get('tier'),
    'column': talent.get('column'),
}, tree_type='spec'))
```

- [ ] **Step 4: 对字符串型 `talentLoadoutText` 不再直出到模板**

```python
def _parse_rio_talents(self, talent_loadout_text):
    if not talent_loadout_text:
        return []
    return [{
        'tree_type': 'code',
        'talent_code': talent_loadout_text,
        'talent_id': None,
        'spell_id': None,
        'name': 'Talent Loadout',
        'icon': '',
        'points': 0,
        'row': None,
        'column': None,
    }]
```

- [ ] **Step 5: 在服务层增加可展示的节点聚合输出**

```python
@staticmethod
def _aggregate_talent_usage(rows):
    usage = {}
    total = len(rows)
    for row in rows:
        for node in (row.talents_json or []):
            key = (node.get('tree_type'), node.get('spell_id') or node.get('talent_id'))
            ...
    return sorted(usage.values(), key=lambda x: x['usage_pct'], reverse=True)
```

- [ ] **Step 6: 提交**

```powershell
git add 'botend/controller/plugins/portal/SpecDetailBase.py' 'botend/controller/plugins/portal/SpecDetailPlayerMonitor.py' 'botend/services/spec_stats_service.py' 'botend/constants/wow.py'
git commit -m 'refactor(spec-detail): normalize talent data structures'
```

## Task 6: 重做人物榜和玩家详情页

**Files:**
- Modify: `botend/portal/spec_detail_views.py`
- Modify: `botend/templates/portal/spec_detail/player_list.html`
- Modify: `botend/templates/portal/spec_detail/player_detail.html`
- Modify: `static/portal/css/portal.css`

- [ ] **Step 1: 在视图上下文中加入更新时间、样本量和天赋展示数据**

```python
ctx.update({
    'sample_size': len(player_data.get('players', [])),
    'updated_at': ...,
})
```

- [ ] **Step 2: 重做人物榜模板结构**

```html
<section class="spec-hero-card">
  <div class="spec-hero-meta">
    <h1>{{ class_name }} {{ spec_name }}</h1>
    <p>全球 Top 20 · 数据来源 Raider.IO</p>
  </div>
</section>

<section class="player-ranking-card">
  {% for player in players %}
  <a class="player-ranking-row" href="{% url 'spec_detail_player_detail' class_name spec_name player.id %}">
    <span class="rank">#{{ player.rank }}</span>
    <span class="name">{{ player.character_name }}</span>
    <span class="region">{{ player.region }}</span>
    <span class="score">{{ player.score }}</span>
  </a>
  {% endfor %}
</section>
```

- [ ] **Step 3: 重做玩家详情模板中的天赋区域**

```html
<section class="talent-usage-card">
  <h4>天赋配置</h4>
  <div class="talent-grid talent-grid--detail">
    {% for node in player_detail.talents %}
    <div class="talent-node-card">
      {% if node.icon %}
      <img src="{{ node.icon }}" alt="{{ node.name }}">
      {% endif %}
      <div class="talent-node-info">
        <span class="talent-name">{{ node.name|default:'未命名天赋' }}</span>
        {% if node.points %}
        <span class="talent-points">{{ node.points }} 点</span>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
</section>
```

- [ ] **Step 4: 增加页面样式**

```css
.player-ranking-card { background:#121826; border-radius:16px; padding:20px; }
.player-ranking-row { display:grid; grid-template-columns:72px 1fr 90px 120px; gap:12px; }
.talent-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:12px; }
.talent-node-card { display:flex; gap:12px; align-items:center; background:#0f172a; border:1px solid rgba(148,163,184,.16); border-radius:14px; padding:12px; }
```

- [ ] **Step 5: 本地启动页面检查**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py runserver 127.0.0.1:8011 --noreload
```

Expected: 人物榜和玩家详情页能正常访问，且不再出现大面积 `? spellID` 链接。

- [ ] **Step 6: 提交**

```powershell
git add 'botend/portal/spec_detail_views.py' 'botend/templates/portal/spec_detail/player_list.html' 'botend/templates/portal/spec_detail/player_detail.html' 'static/portal/css/portal.css'
git commit -m 'feat(spec-detail): redesign player ranking and detail pages'
```

## Task 7: 重做 M+ / 团本统计页中的天赋展示

**Files:**
- Modify: `botend/templates/portal/spec_detail/dungeon_stats.html`
- Modify: `botend/templates/portal/spec_detail/raid_stats.html`
- Modify: `botend/services/spec_stats_service.py`

- [ ] **Step 1: 在服务层为副本和团本详情加入 `talent_usage`**

```python
return {
    ...
    'talent_usage': SpecStatsService._aggregate_talent_usage(rows),
}
```

- [ ] **Step 2: 在 M+ 详情模板加入天赋热力卡**

```html
<section class="talent-usage-card">
  <h3>热门天赋</h3>
  <div class="talent-usage-list">
    {% for node in dungeon_detail.talent_usage %}
    <div class="talent-usage-row">
      <span class="talent-name">{{ node.name }}</span>
      <span class="talent-pct">{{ node.usage_pct }}%</span>
    </div>
    {% endfor %}
  </div>
</section>
```

- [ ] **Step 3: 在团本详情模板加入同构模块**

```html
<section class="talent-usage-card">
  <h3>热门天赋</h3>
  <div class="talent-usage-list">
    {% for node in boss_detail.talent_usage %}
    <div class="talent-usage-row">
      <span class="talent-name">{{ node.name }}</span>
      <span class="talent-pct">{{ node.usage_pct }}%</span>
    </div>
    {% endfor %}
  </div>
</section>
```

- [ ] **Step 4: 本地访问两个统计页**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py runserver 127.0.0.1:8011 --noreload
```

Expected: 统计页能看到按占比展示的天赋模块。

- [ ] **Step 5: 提交**

```powershell
git add 'botend/templates/portal/spec_detail/dungeon_stats.html' 'botend/templates/portal/spec_detail/raid_stats.html' 'botend/services/spec_stats_service.py'
git commit -m 'feat(spec-detail): add talent usage to dungeon and raid pages'
```

## Task 8: 统一收尾验证并推送

**Files:**
- Modify: 上述所有已改文件

- [ ] **Step 1: 运行 Django 检查**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py check
```

Expected: `System check identified no issues`。

- [ ] **Step 2: 运行人物榜验证命令**

Run:

```powershell
& '.\.venv\Scripts\python.exe' manage.py verify_spec_detail --class-name Monk --spec-name Windwalker
```

Expected: PASS。

- [ ] **Step 3: 手动打开页面检查**

URLs:

```text
http://127.0.0.1:8011/portal/spec/Monk/Windwalker/
http://127.0.0.1:8011/portal/spec/Monk/Windwalker/player/<valid_id>/
http://127.0.0.1:8011/portal/spec/Monk/Windwalker/dungeon/?dungeon_id=<valid_id>
http://127.0.0.1:8011/portal/spec/Monk/Windwalker/raid/?boss_id=<valid_id>
```

Expected: 页面布局正常，榜单正确，天赋信息可读。

- [ ] **Step 4: 提交最终整合修改**

```powershell
git add .
git commit -m 'feat(spec-detail): complete wow portal repair pass'
```

- [ ] **Step 5: 推送**

```powershell
git push origin HEAD
```

Expected: 远程分支更新成功。

