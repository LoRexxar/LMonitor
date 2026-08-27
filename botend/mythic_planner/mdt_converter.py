import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


SOURCE_TAG = '6.2.9'
SOURCE_COMMIT = 'a49bd78f843bb89d2cea3daefbc1cf7aed809c31'
SOURCE_URL = f'https://github.com/Nnoggie/MythicDungeonTools/tree/{SOURCE_TAG}'
MAP_SOURCE_WIDTH = 840
MAP_SOURCE_HEIGHT = 560
MAP_TILE_SIZE = 128
MAP_TILE_COLUMNS = 15
MAP_TILE_ROWS = 10
MAP_OUTPUT_WIDTH = MAP_TILE_SIZE * MAP_TILE_COLUMNS
MAP_OUTPUT_HEIGHT = MAP_TILE_SIZE * MAP_TILE_ROWS
MAP_STATIC_PREFIX = f'/static/portal/mythic_planner/vendor/mdt-{SOURCE_TAG}/maps'
UI_ASSET_FILES = {
    'MDTFull.tga': 'mdt-full.png',
    'icons.tga': 'icons.png',
    'ring.tga': 'ring.png',
    'Circle_White.tga': 'circle-white.png',
    'arrows.tga': 'arrows.png',
    'triangle.tga': 'triangle.png',
    'line.tga': 'line.png',
}
ABILITY_OVERRIDE_RELATIVE_PATH = Path('LMonitor/ability_overrides.json')

MARKER_COLORS = (
    '#d6a84b', '#67b7dc', '#9ac56b', '#c47ae0', '#dc7b6d',
    '#7ac7ba', '#d6ca65', '#8298e8', '#e18fbc', '#8fbc6f',
)

TRAIT_KEYS = {
    'Taunt': 'taunt',
    'Stun': 'stun',
    'Silence': 'silence',
    'Root': 'root',
    'Slow': 'slow',
    'Banish': 'banish',
    'Disorient': 'disorient',
    'Incapacitate': 'incapacitate',
    'Knock': 'knock',
    'Grip': 'grip',
    'Mind Control': 'mind_control',
    'Fear': 'fear',
    'Sleep Walk': 'sleep_walk',
    'Polymorph': 'polymorph',
    'Shackle Undead': 'shackle_undead',
    'Sap': 'sap',
    'Turn Evil': 'turn_evil',
    'Repentance': 'repentance',
    'Paralyze': 'paralyze',
}

DISPEL_FLAGS = (
    ('magic', '魔法'),
    ('curse', '诅咒'),
    ('poison', '中毒'),
    ('disease', '疾病'),
    ('bleed', '流血'),
    ('enrage', '激怒'),
)

POI_LABELS = {
    'dungeonEntrance': '地下城入口',
    'dungeonExit': '地下城出口',
    'portal': '传送点',
    'mapLink': '楼层通道',
}

SEASON_GROUP_PATTERN = re.compile(
    r'tinsert\s*\(\s*MDT\.seasonList\s*,\s*L\[(?P<quote>["\'])'
    r'(?P<label>.+?)(?P=quote)\]\s*\)\s*'
    r'tinsert\s*\(\s*MDT\.dungeonSelectionToIndex\s*,\s*'
    r'(?P<indexes>\{[^{}]*\})\s*\)',
    re.DOTALL,
)


@dataclass(frozen=True)
class LocaleRef:
    key: str


class LuaParseError(ValueError):
    pass


class LuaValueParser:
    """解析 MDT 数据文件中使用的受限 Lua 字面量，不执行任何 Lua 代码。"""

    NUMBER_RE = re.compile(
        r'(?:0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)'
    )
    IDENTIFIER_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

    def __init__(self, text, position=0):
        self.text = text
        self.position = position

    def parse(self):
        value = self.parse_value()
        self.skip_ignored()
        return value

    def skip_ignored(self):
        while self.position < len(self.text):
            if self.text[self.position].isspace():
                self.position += 1
                continue
            if self.text.startswith('--[[', self.position):
                end = self.text.find(']]', self.position + 4)
                if end < 0:
                    raise LuaParseError('Lua 块注释未闭合。')
                self.position = end + 2
                continue
            if self.text.startswith('--', self.position):
                end = self.text.find('\n', self.position + 2)
                self.position = len(self.text) if end < 0 else end + 1
                continue
            break

    def peek(self, value):
        self.skip_ignored()
        return self.text.startswith(value, self.position)

    def expect(self, value):
        self.skip_ignored()
        if not self.text.startswith(value, self.position):
            preview = self.text[self.position:self.position + 40].replace('\n', '\\n')
            raise LuaParseError(f'预期 {value!r}，实际位置内容为 {preview!r}。')
        self.position += len(value)

    def parse_value(self):
        self.skip_ignored()
        if self.position >= len(self.text):
            raise LuaParseError('Lua 值意外结束。')
        current = self.text[self.position]
        if current == '{':
            return self.parse_table()
        if current in {'"', "'"}:
            return self.parse_string()
        if current == '-':
            self.position += 1
            value = self.parse_number()
            if not isinstance(value, (int, float)):
                raise LuaParseError('一元负号后必须是数字。')
            return -value
        if current.isdigit() or current == '.':
            return self.parse_number()
        identifier = self.parse_identifier()
        if identifier == 'true':
            return True
        if identifier == 'false':
            return False
        if identifier == 'nil':
            return None
        if identifier == 'L' and self.peek('['):
            self.expect('[')
            key = self.parse_value()
            self.expect(']')
            if not isinstance(key, str):
                raise LuaParseError('本地化引用的键必须是字符串。')
            return LocaleRef(key)
        raise LuaParseError(f'不支持的 Lua 标识符值：{identifier}。')

    def parse_identifier(self):
        self.skip_ignored()
        match = self.IDENTIFIER_RE.match(self.text, self.position)
        if not match:
            preview = self.text[self.position:self.position + 40].replace('\n', '\\n')
            raise LuaParseError(f'预期 Lua 标识符，实际位置内容为 {preview!r}。')
        self.position = match.end()
        return match.group(0)

    def parse_number(self):
        self.skip_ignored()
        match = self.NUMBER_RE.match(self.text, self.position)
        if not match:
            raise LuaParseError('Lua 数字格式不正确。')
        raw = match.group(0)
        self.position = match.end()
        if raw.lower().startswith('0x'):
            return int(raw, 16)
        if any(character in raw for character in '.eE'):
            return float(raw)
        return int(raw)

    def parse_string(self):
        self.skip_ignored()
        quote = self.text[self.position]
        self.position += 1
        result = []
        escapes = {
            'a': '\a',
            'b': '\b',
            'f': '\f',
            'n': '\n',
            'r': '\r',
            't': '\t',
            'v': '\v',
            '\\': '\\',
            '"': '"',
            "'": "'",
        }
        while self.position < len(self.text):
            character = self.text[self.position]
            self.position += 1
            if character == quote:
                return ''.join(result)
            if character != '\\':
                result.append(character)
                continue
            if self.position >= len(self.text):
                raise LuaParseError('Lua 字符串转义意外结束。')
            escaped = self.text[self.position]
            self.position += 1
            if escaped.isdigit():
                digits = escaped
                while (
                    len(digits) < 3
                    and self.position < len(self.text)
                    and self.text[self.position].isdigit()
                ):
                    digits += self.text[self.position]
                    self.position += 1
                result.append(chr(int(digits, 10)))
            else:
                result.append(escapes.get(escaped, escaped))
        raise LuaParseError('Lua 字符串未闭合。')

    def parse_table(self):
        self.expect('{')
        result = {}
        implicit_index = 1
        while True:
            self.skip_ignored()
            if self.peek('}'):
                self.expect('}')
                return result
            if self.peek('['):
                self.expect('[')
                key = self.parse_value()
                self.expect(']')
                self.expect('=')
                value = self.parse_value()
            else:
                start = self.position
                key = None
                if self.IDENTIFIER_RE.match(self.text, self.position):
                    candidate = self.parse_identifier()
                    if self.peek('='):
                        self.expect('=')
                        key = candidate
                        value = self.parse_value()
                    else:
                        self.position = start
                if key is None:
                    key = implicit_index
                    value = self.parse_value()
                    implicit_index += 1
            result[key] = value
            self.skip_ignored()
            if self.peek(','):
                self.expect(',')
            elif self.peek(';'):
                self.expect(';')
            elif not self.peek('}'):
                raise LuaParseError('Lua 表字段之间缺少逗号或分号。')


def _assignment_value(text, variable_name):
    pattern = re.compile(
        rf'MDT\.{re.escape(variable_name)}\s*\[\s*dungeonIndex\s*\]\s*=\s*'
    )
    match = pattern.search(text)
    if not match:
        raise LuaParseError(f'找不到 MDT.{variable_name}[dungeonIndex] 赋值。')
    return LuaValueParser(text, match.end()).parse()


def _ordered_values(table):
    if not isinstance(table, dict):
        return []
    numeric_items = [
        (key, value)
        for key, value in table.items()
        if isinstance(key, int)
    ]
    return [value for _key, value in sorted(numeric_items)]


def _resolve_locale(value, locale, fallback=''):
    if isinstance(value, LocaleRef):
        return locale.get(value.key, fallback or value.key)
    if value is None:
        return fallback
    return str(value)


def _slug(value):
    normalized = str(value or '').lower().replace("'", '')
    normalized = re.sub(r'[^a-z0-9]+', '-', normalized).strip('-')
    return normalized or 'unknown'


def _load_locale(path):
    entries = {}
    text = Path(path).read_text(encoding='utf-8-sig')
    for line in text.splitlines():
        match = re.match(r'\s*L\s*\[', line)
        if not match:
            continue
        try:
            parser = LuaValueParser(line, match.end())
            key = parser.parse_value()
            parser.expect(']')
            parser.expect('=')
            value = parser.parse_value()
        except LuaParseError:
            continue
        if isinstance(key, str) and isinstance(value, str):
            entries[key] = value
    return entries


def _load_order(midnight_path):
    manifest = Path(midnight_path) / 'load_midnight.xml'
    text = manifest.read_text(encoding='utf-8-sig')
    files = re.findall(
        r'<Script\s+file=["\']([^"\']+\.lua)["\']',
        text,
        flags=re.IGNORECASE,
    )
    return [Path(name.replace('\\', '/')).name for name in files]


def _percent_x(value):
    return round(max(0.0, min(100.0, float(value or 0) / MAP_SOURCE_WIDTH * 100)), 6)


def _percent_y(value):
    return round(max(0.0, min(100.0, -float(value or 0) / MAP_SOURCE_HEIGHT * 100)), 6)


def _json_safe(value):
    if isinstance(value, LocaleRef):
        return value.key
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _normalize_patrol(table):
    result = []
    for point in _ordered_values(table):
        if not isinstance(point, dict):
            continue
        result.append({
            'x': _percent_x(point.get('x')),
            'y': _percent_y(point.get('y')),
            'source_x': point.get('x'),
            'source_y': point.get('y'),
        })
    return result


def _spell_names(spell_id, snapshots):
    snapshot = (snapshots or {}).get(int(spell_id))
    if snapshot:
        return (
            str(snapshot.get('name') or f'Spell #{spell_id}'),
            str(snapshot.get('name_zh') or f'技能 #{spell_id}'),
            str(snapshot.get('description') or ''),
            str(snapshot.get('icon_url') or ''),
        )
    return f'Spell #{spell_id}', f'技能 #{spell_id}', '', ''


def _convert_pois(source_table, locale_zh, spell_snapshots=None):
    result = []
    for source_index, poi in enumerate(_ordered_values(source_table), start=1):
        if not isinstance(poi, dict):
            continue
        info = poi.get('info') if isinstance(poi.get('info'), dict) else {}
        poi_type = str(poi.get('type') or 'note')
        source_name = str(info.get('name') or '')
        try:
            spell_id = int(info.get('spellId') or 0)
        except (TypeError, ValueError):
            spell_id = 0
        snapshot = (spell_snapshots or {}).get(spell_id) or {}
        label = (
            locale_zh.get(source_name, source_name)
            if source_name
            else str(
                snapshot.get('name_zh')
                or snapshot.get('name')
                or POI_LABELS.get(poi_type, '')
            )
        )
        metadata = {
            'source_index': source_index,
            'source_x': poi.get('x'),
            'source_y': poi.get('y'),
            'source': _json_safe(poi),
        }
        if spell_id and any(snapshot.get(field) for field in (
            'name',
            'name_zh',
            'description',
            'description_zh',
            'icon_name',
            'source',
            'data_env',
            'difficulty_id',
            'locales',
        )):
            metadata['tooltip'] = {
                field: _json_safe(snapshot.get(field))
                for field in (
                    'name',
                    'name_zh',
                    'description',
                    'description_zh',
                    'icon_name',
                    'source',
                    'data_env',
                    'difficulty_id',
                    'locales',
                )
                if snapshot.get(field) not in (None, '', [])
            }
        result.append({
            'key': f'poi-{source_index}',
            'type': poi_type,
            'x': _percent_x(poi.get('x')),
            'y': _percent_y(poi.get('y')),
            'label': label,
            'icon_url': str(snapshot.get('icon_url') or ''),
            'target_floor_key': (
                f"floor-{int(poi['sublevel'])}"
                if poi.get('sublevel') not in (None, '')
                else ''
            ),
            'metadata': metadata,
        })
    return result


def _enemy_key(source_enemy, source_index, used_keys):
    npc_id = int(source_enemy.get('id') or 0)
    base = f'npc-{npc_id}' if npc_id else f'enemy-{source_index}'
    key = base
    suffix = 2
    while key in used_keys:
        key = f'{base}-variant-{suffix}'
        suffix += 1
    used_keys.add(key)
    return key


def _load_ability_overrides(source_root):
    source_root = Path(source_root)
    override_path = source_root / ABILITY_OVERRIDE_RELATIVE_PATH
    if not override_path.is_file():
        return {}, {}, None
    try:
        raw = json.loads(override_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise LuaParseError(f'技能补充表不是有效 JSON：{override_path}。') from exc
    if not isinstance(raw, dict) or raw.get('schema_version') != 1:
        raise LuaParseError('技能补充表 schema_version 必须为 1。')
    target = raw.get('target')
    if not isinstance(target, dict):
        raise LuaParseError('技能补充表缺少 target。')
    if target.get('source_tag') != SOURCE_TAG:
        raise LuaParseError(
            f'技能补充表目标标签 {target.get("source_tag")!r} 与 {SOURCE_TAG!r} 不一致。'
        )
    if target.get('source_commit') != SOURCE_COMMIT:
        raise LuaParseError('技能补充表目标提交与固定 MDT 快照不一致。')

    raw_dungeons = raw.get('dungeons')
    if not isinstance(raw_dungeons, dict):
        raise LuaParseError('技能补充表 dungeons 必须为对象。')
    normalized = {}
    for dungeon_key, dungeon_data in raw_dungeons.items():
        if not isinstance(dungeon_data, dict) or not isinstance(
            dungeon_data.get('enemies'),
            dict,
        ):
            raise LuaParseError(f'技能补充表地下城 {dungeon_key!r} 缺少 enemies 对象。')
        enemies = {}
        for raw_npc_id, raw_spell_ids in dungeon_data['enemies'].items():
            try:
                npc_id = int(raw_npc_id)
            except (TypeError, ValueError) as exc:
                raise LuaParseError(
                    f'技能补充表地下城 {dungeon_key!r} 包含无效 NPC ID。'
                ) from exc
            if npc_id <= 0 or not isinstance(raw_spell_ids, list) or not raw_spell_ids:
                raise LuaParseError(
                    f'技能补充表 NPC {npc_id} 的技能列表必须为非空数组。'
                )
            spell_ids = []
            for raw_spell_id in raw_spell_ids:
                try:
                    spell_id = int(raw_spell_id)
                except (TypeError, ValueError) as exc:
                    raise LuaParseError(
                        f'技能补充表 NPC {npc_id} 包含无效技能 ID。'
                    ) from exc
                if spell_id <= 0:
                    raise LuaParseError(f'技能补充表 NPC {npc_id} 的技能 ID 必须大于 0。')
                spell_ids.append(spell_id)
            if len(spell_ids) != len(set(spell_ids)):
                raise LuaParseError(f'技能补充表 NPC {npc_id} 包含重复技能 ID。')
            enemies[npc_id] = sorted(spell_ids)
        normalized[str(dungeon_key)] = enemies

    raw_descriptions = raw.get('spell_descriptions_zh', {})
    if not isinstance(raw_descriptions, dict):
        raise LuaParseError('技能补充表 spell_descriptions_zh 必须为对象。')
    spell_descriptions_zh = {}
    for raw_spell_id, raw_description in raw_descriptions.items():
        try:
            spell_id = int(raw_spell_id)
        except (TypeError, ValueError) as exc:
            raise LuaParseError('技能补充表包含无效的说明技能 ID。') from exc
        description = str(raw_description or '').strip()
        if spell_id <= 0 or not description:
            raise LuaParseError('技能补充表的简中技能说明不能为空。')
        spell_descriptions_zh[spell_id] = description

    metadata = {
        'relative_path': ABILITY_OVERRIDE_RELATIVE_PATH.as_posix(),
        'target': target,
        'sources': raw.get('sources', []),
        'notes_zh': str(raw.get('notes_zh') or ''),
    }
    return normalized, spell_descriptions_zh, metadata


def _convert_enemies(
    source_table,
    locale_zh,
    dungeon_key,
    spell_snapshots=None,
    ability_overrides=None,
    enemy_icon_urls=None,
):
    enemies = []
    used_keys = set()
    ability_overrides = ability_overrides or {}
    seen_override_npc_ids = set()
    for source_index, source_enemy in enumerate(_ordered_values(source_table), start=1):
        if not isinstance(source_enemy, dict):
            continue
        key = _enemy_key(source_enemy, source_index, used_keys)
        npc_id = int(source_enemy.get('id') or 0)
        source_name = str(source_enemy.get('name') or f'Enemy {source_index}')
        characteristics = (
            source_enemy.get('characteristics')
            if isinstance(source_enemy.get('characteristics'), dict)
            else {}
        )
        traits = {
            TRAIT_KEYS.get(str(name), _slug(name).replace('-', '_')): bool(allowed)
            for name, allowed in characteristics.items()
        }
        abilities = []
        source_spells = (
            source_enemy.get('spells')
            if isinstance(source_enemy.get('spells'), dict)
            else {}
        )
        ability_source = 'MythicDungeonTools'
        if npc_id in ability_overrides:
            source_spells = {
                spell_id: {}
                for spell_id in ability_overrides[npc_id]
            }
            seen_override_npc_ids.add(npc_id)
            ability_source = 'LMonitorAbilitySupplement'
        for ability_order, (spell_id, flags) in enumerate(
            sorted(source_spells.items(), key=lambda item: int(item[0])),
            start=1,
        ):
            spell_id = int(spell_id)
            flags = flags if isinstance(flags, dict) else {}
            name, name_zh, description_zh, icon_url = _spell_names(
                spell_id,
                spell_snapshots,
            )
            dispel_types = [label for flag, label in DISPEL_FLAGS if flags.get(flag)]
            interruptible = bool(flags.get('interruptible'))
            if interruptible:
                traits['interrupt'] = True
            if flags.get('enrage'):
                traits['soothe'] = True
            danger_level = 3 if interruptible else (2 if dispel_types or flags else 1)
            abilities.append({
                'spell_id': spell_id,
                'name': name,
                'name_zh': name_zh,
                'description_zh': description_zh,
                'icon_url': icon_url,
                'interruptible': interruptible,
                'dispel_type': '、'.join(dispel_types),
                'danger_level': danger_level,
                'order': ability_order,
                'metadata': {
                    'source_flags': _json_safe(flags),
                    'source': ability_source,
                },
            })

        spawns = []
        clones = (
            source_enemy.get('clones')
            if isinstance(source_enemy.get('clones'), dict)
            else {}
        )
        for clone_index, clone in sorted(
            (
                (int(index), clone)
                for index, clone in clones.items()
                if isinstance(index, int) and isinstance(clone, dict)
            ),
            key=lambda item: item[0],
        ):
            clone_scale = clone.get('scale', 1)
            enemy_scale = source_enemy.get('scale', 1)
            try:
                scale = (
                    float(clone_scale or 1)
                    * float(enemy_scale or 1)
                    * (1.7 if source_enemy.get('isBoss') else 1)
                    * 0.6
                )
                scale = max(0.25, min(5.0, scale))
            except (TypeError, ValueError):
                scale = 0.6
            group = clone.get('g')
            spawns.append({
                'key': f'clone-{clone_index}',
                'floor_key': f"floor-{int(clone.get('sublevel') or 1)}",
                'x': _percent_x(clone.get('x')),
                'y': _percent_y(clone.get('y')),
                'group_key': f'group-{group}' if group not in (None, '') else '',
                'scale': scale,
                'patrol': _normalize_patrol(clone.get('patrol')),
                'metadata': {
                    'source_clone_index': clone_index,
                    'source_x': clone.get('x'),
                    'source_y': clone.get('y'),
                    'source_group': group,
                    'source_sublevel': clone.get('sublevel', 1),
                },
            })

        enemies.append({
            'key': key,
            'npc_id': npc_id or None,
            'name': source_name,
            'name_zh': locale_zh.get(source_name, ''),
            'enemy_forces': int(source_enemy.get('count') or 0),
            'base_health': int(source_enemy.get('health') or 0),
            'level': max(0, int(source_enemy.get('level') or 0)),
            'creature_type': str(source_enemy.get('creatureType') or ''),
            'icon_url': str(
                (enemy_icon_urls or {}).get((dungeon_key, key)) or ''
            ),
            'marker_color': MARKER_COLORS[(source_index - 1) % len(MARKER_COLORS)],
            'is_boss': bool(source_enemy.get('isBoss')),
            'traits': traits,
            'abilities': abilities,
            'spawns': spawns,
            'metadata': {
                'source_enemy_index': source_index,
                'display_id': source_enemy.get('displayId'),
                'source_scale': source_enemy.get('scale'),
                'source_level': source_enemy.get('level'),
                'source_characteristics': _json_safe(characteristics),
            },
        })
    unknown_npc_ids = sorted(set(ability_overrides) - seen_override_npc_ids)
    if unknown_npc_ids:
        raise LuaParseError(
            '技能补充表包含当前地下城不存在的 NPC：'
            + '、'.join(str(npc_id) for npc_id in unknown_npc_ids)
            + '。'
        )
    return enemies


def _source_digest(source_root):
    digest = hashlib.sha256()
    source_root = Path(source_root)
    relative_paths = [
        Path('LICENSE'),
        Path('Locales/enUS.lua'),
        Path('Locales/zhCN.lua'),
        Path('Midnight/load_midnight.xml'),
        Path('Modules/DungeonSelect.lua'),
    ]
    relative_paths.extend(
        sorted(path.relative_to(source_root) for path in (source_root / 'Midnight').glob('*.lua'))
    )
    override_path = source_root / ABILITY_OVERRIDE_RELATIVE_PATH
    if override_path.is_file():
        relative_paths.append(ABILITY_OVERRIDE_RELATIVE_PATH)
    for relative_path in relative_paths:
        digest.update(relative_path.as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update((source_root / relative_path).read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def _load_selection_groups(source_root, locale_zh):
    source_path = Path(source_root) / 'Modules' / 'DungeonSelect.lua'
    if not source_path.exists():
        raise LuaParseError('Modules/DungeonSelect.lua 不存在，无法读取 MDT 赛季分类。')
    source_text = source_path.read_text(encoding='utf-8-sig')
    groups = []
    for group_order, match in enumerate(SEASON_GROUP_PATTERN.finditer(source_text), start=1):
        source_label = match.group('label')
        raw_indexes = LuaValueParser(match.group('indexes')).parse()
        indexes = [
            int(index)
            for index in _ordered_values(raw_indexes)
            if isinstance(index, (int, float))
        ]
        if not indexes:
            continue
        groups.append({
            'key': _slug(source_label),
            'name': source_label,
            'name_zh': locale_zh.get(source_label, source_label),
            'order': group_order,
            'dungeon_indexes': indexes,
        })
    if not groups:
        raise LuaParseError('Modules/DungeonSelect.lua 中未找到 MDT 赛季分类。')
    return groups


def build_payload(
    source_root,
    *,
    version_key=None,
    spell_snapshots=None,
    floor_background_urls=None,
    enemy_icon_urls=None,
):
    source_root = Path(source_root).resolve()
    midnight_path = source_root / 'Midnight'
    locale_en = _load_locale(source_root / 'Locales' / 'enUS.lua')
    locale_zh = _load_locale(source_root / 'Locales' / 'zhCN.lua')
    selection_groups = _load_selection_groups(source_root, locale_zh)
    (
        ability_overrides,
        spell_descriptions_zh,
        ability_override_metadata,
    ) = _load_ability_overrides(source_root)
    effective_spell_snapshots = {
        int(spell_id): dict(snapshot)
        for spell_id, snapshot in (spell_snapshots or {}).items()
    }
    for spell_id, description_zh in spell_descriptions_zh.items():
        snapshot = dict(effective_spell_snapshots.get(spell_id) or {})
        snapshot['description'] = description_zh
        effective_spell_snapshots[spell_id] = snapshot
    group_memberships = {}
    for group in selection_groups:
        for dungeon_order, dungeon_index in enumerate(group['dungeon_indexes'], start=1):
            group_memberships.setdefault(dungeon_index, []).append({
                'key': group['key'],
                'name': group['name'],
                'name_zh': group['name_zh'],
                'order': group['order'],
                'dungeon_order': dungeon_order,
            })
    dungeons = []

    for order, filename in enumerate(_load_order(midnight_path), start=1):
        lua_path = midnight_path / filename
        text = lua_path.read_text(encoding='utf-8-sig')
        index_match = re.search(r'local\s+dungeonIndex\s*=\s*(\d+)', text)
        if not index_match:
            raise LuaParseError(f'{filename} 缺少 dungeonIndex。')
        dungeon_index = int(index_match.group(1))
        map_info = _assignment_value(text, 'mapInfo')
        sublevels = _assignment_value(text, 'dungeonSubLevels')
        total_count = _assignment_value(text, 'dungeonTotalCount')
        map_pois = _assignment_value(text, 'mapPOIs')
        source_enemies = _assignment_value(text, 'dungeonEnemies')
        if not all(
            isinstance(value, dict)
            for value in (map_info, sublevels, total_count, map_pois, source_enemies)
        ):
            raise LuaParseError(f'{filename} 的 MDT 数据结构不是 Lua 表。')

        english_name = _resolve_locale(
            map_info.get('englishName'),
            locale_en,
            fallback=lua_path.stem,
        )
        dungeon_key = _slug(english_name)
        floors = []
        for floor_index, source_floor_name in sorted(
            (
                (int(key), value)
                for key, value in sublevels.items()
                if isinstance(key, int) and int(key) > 0
            ),
            key=lambda item: item[0],
        ):
            floor_key = f'floor-{floor_index}'
            name = _resolve_locale(source_floor_name, locale_en, fallback=f'Floor {floor_index}')
            name_zh = _resolve_locale(source_floor_name, locale_zh, fallback='')
            floor_pois = map_pois.get(floor_index, {})
            floors.append({
                'key': floor_key,
                'floor_index': floor_index,
                'name': name,
                'name_zh': name_zh,
                'background_url': (
                    (floor_background_urls or {}).get((dungeon_key, floor_key))
                    or f'{MAP_STATIC_PREFIX}/{dungeon_key}/{floor_key}.webp'
                ),
                'background_color': '#171512',
                'map_width': 1200,
                'map_height': 800,
                'order': floor_index,
                'metadata': {
                    'source_texture_directory': lua_path.stem,
                    'source_map_width': MAP_SOURCE_WIDTH,
                    'source_map_height': MAP_SOURCE_HEIGHT,
                    'tile_columns': MAP_TILE_COLUMNS,
                    'tile_rows': MAP_TILE_ROWS,
                    'tile_size': MAP_TILE_SIZE,
                },
                'pois': _convert_pois(
                    floor_pois,
                    locale_zh,
                    effective_spell_snapshots,
                ),
            })

        short_name_ref = map_info.get('shortName')
        short_name = _resolve_locale(short_name_ref, locale_zh).strip()
        dungeons.append({
            'key': dungeon_key,
            'external_index': dungeon_index,
            'name': english_name,
            'name_zh': locale_zh.get(
                sublevels.get(1).key
                if isinstance(sublevels.get(1), LocaleRef)
                else english_name,
                '',
            ),
            'short_name': short_name[:32],
            'map_id': map_info.get('mapID'),
            'total_enemy_forces': int(total_count.get('normal') or 0),
            'order': order,
            'metadata': {
                'source_file': f'Midnight/{filename}',
                'teleport_id': map_info.get('teleportId'),
                'source_tag': SOURCE_TAG,
                'source_commit': SOURCE_COMMIT,
                'selection_groups': group_memberships.get(dungeon_index, []),
            },
            'floors': floors,
            'enemies': _convert_enemies(
                source_enemies,
                locale_zh,
                dungeon_key,
                spell_snapshots=effective_spell_snapshots,
                ability_overrides=ability_overrides.get(dungeon_key),
                enemy_icon_urls=enemy_icon_urls,
            ),
        })

    return {
        'schema_version': 1,
        'data_version': {
            'key': version_key or f'mdt-{SOURCE_TAG.replace(".", "-")}',
            'label': f'MythicDungeonTools {SOURCE_TAG} 午夜版本数据',
            'game_version': 'Midnight',
            'season': f'MythicDungeonTools {SOURCE_TAG}',
            'source_name': 'MythicDungeonTools',
            'source_reference': SOURCE_URL,
            'notes': (
                '由固定上游快照的 Lua 副本数据离线转换生成；地图由原始 15×10 PNG '
                '切片无损拼接。三个上游暂缺技能表的旧副本使用版本化补充关系；技能名称'
                '未匹配到本地法术快照时保留技能 ID。'
            ),
            'metadata': {
                'license': 'GPL-2.0-only',
                'source_tag': SOURCE_TAG,
                'source_commit': SOURCE_COMMIT,
                'source_digest': _source_digest(source_root),
                'source_url': SOURCE_URL,
                'locale': ['enUS', 'zhCN'],
                'generated': True,
                'dungeon_selection_groups': selection_groups,
                'ability_supplement': ability_override_metadata,
            },
        },
        'dungeons': dungeons,
    }


def write_payload(payload, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + '\n'
    output_path.write_text(rendered, encoding='utf-8')
    return output_path


def compose_maps(source_root, static_map_root):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('合成地图需要 Pillow：python -m pip install Pillow') from exc

    source_root = Path(source_root).resolve()
    texture_root = source_root / 'Midnight' / 'Textures'
    static_map_root = Path(static_map_root).resolve()
    manifest = []
    for texture_directory in sorted(path for path in texture_root.iterdir() if path.is_dir()):
        floor_indices = sorted({
            int(match.group(1))
            for path in texture_directory.glob('*.png')
            if (match := re.match(r'^(\d+)_(\d+)\.png$', path.name))
        })
        dungeon_key = None
        for lua_path in (source_root / 'Midnight').glob('*.lua'):
            text = lua_path.read_text(encoding='utf-8-sig')
            if texture_directory.name not in text:
                continue
            map_info = _assignment_value(text, 'mapInfo')
            dungeon_key = _slug(map_info.get('englishName') or lua_path.stem)
            break
        if not dungeon_key:
            raise RuntimeError(f'无法为贴图目录 {texture_directory.name} 找到地下城。')

        output_directory = static_map_root / dungeon_key
        output_directory.mkdir(parents=True, exist_ok=True)
        for floor_index in floor_indices:
            canvas = Image.new('RGBA', (MAP_OUTPUT_WIDTH, MAP_OUTPUT_HEIGHT))
            normalized_tiles = []
            for row in range(MAP_TILE_ROWS):
                for column in range(MAP_TILE_COLUMNS):
                    suffix = row * MAP_TILE_COLUMNS + column + 1
                    tile_path = texture_directory / f'{floor_index}_{suffix}.png'
                    if not tile_path.is_file():
                        raise RuntimeError(f'地图切片缺失：{tile_path}')
                    with Image.open(tile_path) as tile_source:
                        tile = tile_source.convert('RGBA')
                        if tile.size != (MAP_TILE_SIZE, MAP_TILE_SIZE):
                            normalized_tiles.append({
                                'file': tile_path.name,
                                'source_size': list(tile.size),
                            })
                            tile = tile.resize(
                                (MAP_TILE_SIZE, MAP_TILE_SIZE),
                                Image.Resampling.LANCZOS,
                            )
                        canvas.paste(
                            tile,
                            (column * MAP_TILE_SIZE, row * MAP_TILE_SIZE),
                        )
            output_path = output_directory / f'floor-{floor_index}.webp'
            canvas.save(output_path, 'WEBP', lossless=True, method=6)
            manifest.append({
                'dungeon_key': dungeon_key,
                'texture_directory': texture_directory.name,
                'floor_index': floor_index,
                'source_tiles': MAP_TILE_COLUMNS * MAP_TILE_ROWS,
                'output': output_path.as_posix(),
                'output_width': MAP_OUTPUT_WIDTH,
                'output_height': MAP_OUTPUT_HEIGHT,
                'normalized_tiles': normalized_tiles,
                'sha256': hashlib.sha256(output_path.read_bytes()).hexdigest(),
            })
    return manifest


def compose_ui_assets(source_root, static_asset_root):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('转换界面贴图需要 Pillow：python -m pip install Pillow') from exc

    source_root = Path(source_root).resolve()
    texture_root = source_root / 'Textures'
    static_asset_root = Path(static_asset_root).resolve()
    static_asset_root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for source_name, output_name in UI_ASSET_FILES.items():
        source_path = texture_root / source_name
        if not source_path.is_file():
            raise RuntimeError(f'界面贴图缺失：{source_path}')
        output_path = static_asset_root / output_name
        with Image.open(source_path) as image:
            converted = image.convert('RGBA')
            converted.save(output_path, 'PNG', optimize=True)
        manifest.append({
            'source': f'Textures/{source_name}',
            'output': output_path.as_posix(),
            'sha256': hashlib.sha256(output_path.read_bytes()).hexdigest(),
        })
    return manifest
