"""从已固定的 SimC 数据生成基准装备规则；不修改引擎。"""
import json
import re
from pathlib import Path


def build(root):
    generated = root / 'simc-source/engine/dbc/generated'
    embellishments, effects, sets = {}, set(), {}
    for suffix in ('', '_ptr'):
        source = (generated / f'embellishment_data{suffix}.inc').read_text(encoding='utf-8')
        for name, bonus, _effect, spell in re.findall(
            r'\{\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\}', source,
        ):
            # 调味袋与消除美化的粉末不占用美化名额。
            if int(spell) in (372120, 400698, 463429):
                continue
            token = re.sub(r'[^a-z0-9_]', '', name.lower().replace(' ', '_'))
            embellishments[token] = {'bonus_id': int(bonus), 'spell_id': int(spell)}
            effects.add(int(spell))
        source = (generated / f'item_set_bonus{suffix}.inc').read_text(encoding='utf-8')
        for line in source.splitlines():
            match = re.search(r'\{\s*"[^"]+"\s*,\s*"([^"]+)"\s*,\s*"[^"]+"\s*,(.*?)\{(.*?)\}', line)
            if not match:
                continue
            fields = [int(value.strip()) for value in match[2].strip().rstrip(',').split(',')]
            if fields[4] != -1:  # 职业套装仍由既有显式配置控制。
                continue
            ids = [int(value) for value in match[3].split(',') if int(value.strip())]
            sets[(match[1], fields[2])] = {'name': match[1], 'pieces': fields[2], 'items': ids}
    for path in (root / 'simc-source/engine/player').glob('unique_gear_*.cpp'):
        source = path.read_text(encoding='utf-8')
        for numbers in re.findall(r'register_special_effect\(\s*([\d\s{},]+),\s*embellishments::', source):
            effects.update(int(value) for value in re.findall(r'\d+', numbers))
    catalog = json.loads((root / 'simc_embellishment_catalog.json').read_text(encoding='utf-8'))
    intrinsic = [row['item_id'] for row in catalog['items'] if row['category'] == 512 and row['quantity'] == 2]
    return {'version': 2, 'embellishments': embellishments, 'effect_ids': sorted(effects),
            'intrinsic_item_ids': sorted(intrinsic),
            'sets': sorted(sets.values(), key=lambda row: (row['name'], row['pieces']))}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    (root / 'simc_equipment_rules.json').write_text(
        json.dumps(build(root), ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
