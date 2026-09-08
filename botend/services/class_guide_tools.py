"""将攻略中的装备快照转为站内配装器分享状态。"""

import base64
import gzip
import json
from urllib.parse import urlencode

from botend.constants.wow import canonical_class_spec

SLOT_KEYS = {0: 'head', 1: 'neck', 2: 'shoulder', 4: 'chest', 5: 'waist', 6: 'legs', 7: 'feet',
             8: 'wrists', 9: 'hands', 10: 'finger1', 11: 'finger2', 12: 'trinket1', 13: 'trinket2',
             14: 'back', 15: 'main_hand', 16: 'off_hand'}


def gear_tool_url(decoded, guide, refs):
    class_name, spec_name = canonical_class_spec(guide.class_name, guide.spec_name) or ('', '')
    equipment = {}
    def enhancement(object_id, kind='item'):
        ref = refs.get('[[{}:{}]]'.format(kind, object_id), {})
        return {'item': {'item_id': object_id, 'name': ref.get('name', '待校订物品 {}'.format(object_id)),
                         'icon_url': ref.get('icon', '')}, 'variant': {'stats': {}, 'effects': []}, 'external': True}
    for raw_slot, value in decoded.get('items', {}).items():
        slot = SLOT_KEYS.get(int(raw_slot))
        if not slot or value.get('type') != 'item':
            continue
        item = value['item']; object_id = item['id']
        ref = refs.get('[[item:{}]]'.format(object_id), {})
        raw = 'id={},ilevel={}'.format(object_id, item.get('itemLevel', 0))
        if item.get('bonuses'):
            raw += ',bonus_id=' + '/'.join(map(str, item['bonuses']))
        if item.get('gems'):
            raw += ',gem_id=' + '/'.join(map(str, item['gems']))
        equipment[slot] = {'item': {'item_id': object_id, 'name': ref.get('name', '待校订物品 {}'.format(object_id)),
            'icon_url': ref.get('icon', ''), 'slot_key': slot},
            'variant': {'item_level': item.get('itemLevel', 0), 'bonus_ids': item.get('bonuses', []), 'stats': {}, 'effects': []},
            'selectedStats': [], 'resolvedStats': None, 'resolvedEffects': None, 'embellishment': None,
            'gems': [enhancement(i) for i in item.get('gems', [])], 'enchant': None,
            'external': True, 'rawValue': raw, 'sourceModifiers': item.get('modItems', []),
            'sourceSpellModifiers': item.get('modSpells', []), 'sourceSnapshot': item, 'addedSocket': False}
    state = {'version': 1, 'className': class_name, 'specName': spec_name, 'batchKey': '', 'selectedSlot': 'head',
        'mode': 'equipment', 'viewMode': 'editor', 'mobileView': 'browser', 'equipment': equipment}
    code = 'z' + base64.urlsafe_b64encode(gzip.compress(json.dumps(state, ensure_ascii=False).encode(), mtime=0)).decode().rstrip('=')
    return '/portal/gear-builder/?' + urlencode({'code': code})
