"""分离历史物品提示中的描述、静态属性与特效，不推断或改写属性数值。"""
import re
import unicodedata
from functools import lru_cache


ENHANCEMENTS = {'gem', 'enchant', 'embellishment'}
STAT_NAMES = {
    'strength': ('力量', 'Strength'), 'agility': ('敏捷', 'Agility'),
    'intellect': ('智力', 'Intellect'), 'stamina': ('耐力', 'Stamina'),
    'armor': ('护甲', 'Armor'), 'crit': ('暴击', '爆击', 'Critical Strike', 'Crit'),
    'haste': ('急速', 'Haste'), 'mastery': ('精通', 'Mastery', 'Mast'),
    'versatility': ('全能', 'Versatility', 'Vers'), 'primary': ('主属性', '主要属性', 'Primary', '敏捷或力量', '力量或敏捷', 'Agi/Str'),
    'speed': ('速度', '加速', 'Speed'), 'leech': ('吸血', 'Leech'),
    'avoidance': ('闪避', '躲闪', 'Avoidance'),
}
EFFECT_PREFIX = re.compile(r'^(?:(?:装备|使用|被动|效果|提供下列属性|Equip|Use|Passive|Effect)\s*[:：]|\([24]\)\s*(?:组合|套装|Set))', re.I)


@lru_cache(maxsize=2048)
def _equipment_description_lines(text):
    """排除旧装备 Tooltip 的结构行，不按当前变体数值判断是否为描述。"""
    labels = sorted({label for values in STAT_NAMES.values() for label in values} | {
        '额外护甲', 'Bonus Armor', '武器秒伤', 'Damage Per Second', '每秒伤害',
    }, key=len, reverse=True)
    equipment_types = (
        '头部', '颈部', '肩部', '背部', '胸部', '腕部', '手部', '腰部', '腿部', '脚部',
        '手指', '戒指', '饰品', '单手', '双手', '主手', '副手', '远程',
        '布甲', '皮甲', '锁甲', '板甲', '盾牌', '斧', '剑', '锤', '匕首', '法杖',
        '长柄武器', '战刃', '拳套', '弓', '弩', '枪械', '魔杖', '持在副手',
        'Head', 'Neck', 'Shoulder', 'Back', 'Chest', 'Wrist', 'Hands', 'Waist', 'Legs',
        'Feet', 'Finger', 'Trinket', 'One-Hand', 'Two-Hand', 'Main Hand', 'Off Hand',
        'Held In Off-hand', 'Ranged', 'Cloth', 'Leather', 'Mail', 'Plate', 'Shield',
        'Axe', 'Sword', 'Mace', 'Dagger', 'Staff', 'Polearm', 'Warglaives', 'Fist Weapon',
        'Bow', 'Crossbow', 'Gun', 'Wand',
    )
    type_row = re.compile(r'(?:(?:' + '|'.join(map(re.escape, equipment_types)) + r')\s*)+', re.I)
    metadata_row = re.compile(
        r'^(?:(?:升级|Upgrade|耐久|Durability|掉落于|Dropped by|掉落几率|Drop Chance|'
        r'来源|Source|职业|Classes?|种族|Races?|装备唯一|Unique-Equipped)\s*[:：]|'
        r'(?:需要等级|Requires Level)\s*\d|'
        r'(?:拾取后绑定|装备后绑定|战团绑定|Binds when picked up|Binds when equipped)\s*$)', re.I,
    )
    lines = []
    for raw in str(text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line = raw.strip()
        value = unicodedata.normalize('NFKC', line)
        value = re.sub(r'^静态属性说明\s*:\s*', '', value)
        if metadata_row.match(value) or type_row.fullmatch(value):
            continue
        if re.fullmatch(r'(?:史诗钥石|史诗|稀有|精良|优秀|普通|Mythic Keystone|Epic|Rare|Uncommon|Common)', value, re.I):
            continue
        # 旧规范化文本把“耐久 50 / 50”拆成两行，第二行可能还带等级要求。
        if re.fullmatch(r'\d+(?:\s+(?:需要等级|Requires Level)\s*\d+)?', value, re.I):
            continue
        stat = re.fullmatch(r'\+?\s*[\d,.]+\s*(.+)', value)
        if stat:
            remainder = re.sub(r'随机属性\s*\d+|Random (?:Stat|Enchantment)\s*\d*', '', stat[1], flags=re.I)
            for label in labels:
                remainder = re.sub(re.escape(label), '', remainder, flags=re.I)
            remainder = re.sub(r'\b(?:or|and)\b|[\s\[\]()或和与及、,/&+点]+', '', remainder, flags=re.I)
            if not remainder:
                continue
        if re.fullmatch(r'[\d,.]+\s*-\s*[\d,.]+\s*(?:伤害|Damage)', value, re.I):
            continue
        lines.append(line)
    return tuple(lines)


def clean_text(text, names=()):
    value = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'(?:物品等级|Item Level)\s*[:：]?\s*[\d,.]+', '', value, flags=re.I)
    value = re.sub(r'(?:最大叠加|最大堆叠|Max(?:imum)? Stack(?: Size)?)\s*[:：]?\s*[\d,]+', '', value, flags=re.I)
    value = re.sub(r'(?:售价|Sell Price)\s*[:：]?\s*(?:[\d,.]+\s*(?:金币?|银币?|铜币?|gold|silver|copper)?\s*)+', '', value, flags=re.I).strip()
    for name in filter(None, names):
        stripped = re.sub(r'^' + re.escape(str(name)) + r'(?:\s+|$)', '', value).strip()
        value = '' if stripped != value and re.fullmatch(r'[1-5]', stripped) else stripped
    category = re.match(r'^[^\W\d_]+(?=\s|$)', value)
    if category and (category[0] == 'PvP' or any(str(name).endswith(category[0]) for name in filter(None, names))):
        value = value[len(category[0]):].strip()
    value = re.sub(r'(?:使用|装备|效果|被动|提供下列属性|Use|Equip|Effect|Passive)\s*[:：]\s*(?=$|\n)', '', value, flags=re.I)
    value = re.sub(r'((?:装备唯一|Unique-Equipped)\s*[:：][^\n]*?[（(]\d+[）)])\s*', r'\1\n', value, flags=re.I)
    return value.strip()


def _identity(text):
    value = unicodedata.normalize('NFKC', str(text)).casefold()
    value = re.sub(r'^(?:装备|使用|被动|效果|equip|use|passive|effect)\s*:\s*', '', value)
    return re.sub(r'\s+|[。.!！]+$', '', value)


def _strip_stats(text, stats, metadata):
    values = dict(stats or {})
    primary = (metadata or {}).get('primary_stat_amount')
    if not primary:
        primary = next((values.get(key) for key in ('strength', 'agility', 'intellect') if values.get(key)), None)
    if primary:
        values['primary'] = primary
    patterns = []
    for key, amount in values.items():
        if key not in STAT_NAMES or not isinstance(amount, (int, float)) or not amount:
            continue
        digits = re.escape(f'{amount:g}')
        names = '|'.join(re.escape(label) for label in STAT_NAMES[key])
        patterns.append(rf'(?:\+?\s*{digits}\s*(?:点\s*)?(?:{names})(?![A-Za-z])|(?:{names})\s*\+?\s*{digits}(?![\d.])|(?:使?(?:你的)?|永久使该装备的)(?:{names})\s*(?:提高|增加)\s*{digits}(?![\d.])\s*点?|该装备也会额外获得{digits}点(?:{names})(?:值)?)')
    value = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', text)
    removed = False
    if patterns:
        pattern = re.compile(r'^(?:' + '|'.join(patterns) + r')[\s、,，/&和与及。.!！]*', re.I)
        while (match := pattern.match(value)):
            removed = True
            value = value[match.end():].strip()
    return value, removed


def separate_item_text(*, description='', description_zh='', effects=(), stats=None, metadata=None,
                       names=(), enhancement=False, recover_description_effects=False):
    """历史描述只恢复明确的特效；有装等的装备以变体特效为准，避免串用其他装等。"""
    result = {'description': '', 'description_zh': '', 'effects': []}
    seen = set()
    usage = {'description': [], 'description_zh': []}
    stat_only = re.compile(r'\+?\s*[\d,.]+\s*(?:' + '|'.join(re.escape(label) for names_ in STAT_NAMES.values() for label in names_) + r')', re.I)

    def is_unmapped_static(text):
        return bool(stat_only.fullmatch(text) or re.fullmatch(r'(?:永久使该装备的.+?提高\d+点|该装备也会额外获得\d+点[^。]+)[。]?', text))

    def effect_lines(text):
        if enhancement:
            if re.fullmatch(r'Enchant [A-Za-z ]+ - [^.!?]+', text):
                return []
            text = re.sub(r'^(?:使用\s*[:：]\s*)?永久性?地?为[^，。\n]*附魔[^，。\n]*[，。]', '', text)
            text = re.sub(r'^使用\s*[:：]\s*在你的腿部装备上附加[^，]+，', '', text)
            text = re.sub(r'(?:不能|无法)对物品等级低于\d+的物品使用[。.]?', '', text)
        return re.split(r'\n|\s+[·/]\s+', text)

    for raw in effects or []:
        row = dict(raw) if isinstance(raw, dict) else {'description': str(raw)}
        for field in ('description', 'description_zh'):
            if field not in row:
                continue
            lines = []
            if enhancement:
                usage[field].extend(re.findall(r'(?:不能|无法)对物品等级低于\d+的物品使用[。.]?', row[field]))
            for line in effect_lines(clean_text(row[field], names)):
                remainder, _removed = _strip_stats(line.strip(), stats, metadata)
                if remainder:
                    if enhancement and is_unmapped_static(remainder):
                        usage[field].append(f'静态属性说明：{remainder}')
                    else:
                        lines.append(remainder)
            row[field] = ' · '.join(lines)
        text = row.get('description_zh') or row.get('description') or row.get('name_zh') or row.get('name') or ''
        identity = _identity(text)
        if identity and identity not in seen:
            seen.add(identity)
            result['effects'].append(row)
    has_variant_effects = bool(result['effects'])
    recovered = {'description': [], 'description_zh': []}
    for field, raw in [('description', description), ('description_zh', description_zh)]:
        descriptions = []
        if not enhancement:
            raw = '\n'.join(_equipment_description_lines(raw))
        if enhancement:
            usage[field].extend(re.findall(r'(?:不能|无法)对物品等级低于\d+的物品使用[。.]?', str(raw)))
        application = bool(re.match(r'(?:使用\s*[:：]\s*)?永久性?地?为', clean_text(raw, names)))
        for line_index, line in enumerate(effect_lines(clean_text(raw, names))):
            line = line.strip()
            remainder, removed = _strip_stats(line, stats, metadata)
            if not remainder:
                continue
            # 未映射的静态数值单独标注，不冒充特效，也不猜测写入属性总计。
            if is_unmapped_static(remainder):
                descriptions.append(f'静态属性说明：{remainder}')
                continue
            is_effect = EFFECT_PREFIX.match(remainder) or (enhancement and (removed or (application and line_index == 0)))
            if is_effect:
                can_localize_single = (enhancement and field == 'description_zh' and len(result['effects']) == 1
                    and not result['effects'][0].get('description_zh'))
                if recover_description_effects and (not has_variant_effects or can_localize_single) and _identity(remainder) not in seen:
                    seen.add(_identity(remainder))
                    recovered[field].append({field: remainder})
            elif _identity(remainder) not in seen:
                descriptions.append(remainder)
        result[field] = '\n'.join(dict.fromkeys([*descriptions, *usage[field]]))
    if has_variant_effects and len(result['effects']) == len(recovered['description_zh']) == 1:
        # 同一强化物品只有一个效果时，补上原说明中明确的中文效果，保留英文及来源字段。
        result['effects'][0]['description_zh'] = recovered['description_zh'][0]['description_zh']
    elif not has_variant_effects:
        result['effects'].extend(recovered['description_zh'] or recovered['description'])
    return result


def normalize_catalog_text(item):
    """在写入目录前分离文本，并保存原始描述供追溯。"""
    raw = {field: item.get(field) or '' for field in ('description', 'description_zh')}
    enhancement = item.get('catalog_type') in ENHANCEMENTS
    names = (item.get('name'), item.get('name_zh'))
    variants = item.get('variants') or []
    descriptions = {field: [] for field in raw}
    for variant in variants or [{}]:
        separated = separate_item_text(**raw, names=names, stats=variant.get('stats'),
            metadata=variant.get('metadata'), effects=variant.get('effects') or [],
            enhancement=enhancement, recover_description_effects=enhancement)
        if variants:
            if separated['effects'] != (variant.get('effects') or []):
                variant.setdefault('metadata', {}).setdefault('raw_effects', variant.get('effects') or [])
            variant['effects'] = separated['effects']
            variant.setdefault('metadata', {})['text_schema_version'] = 2
        for field in raw:
            if separated[field] and separated[field] not in descriptions[field]:
                descriptions[field].append(separated[field])
    for field in raw:
        item[field] = '\n'.join(descriptions[field])
    if any(raw.values()):
        item.setdefault('metadata', {}).setdefault('raw_item_descriptions', raw)
    item.setdefault('metadata', {})['text_schema_version'] = 2
    return item
