"""解析来源组件的低位优先编码和 Snappy 数据，保留装备变体与循环分支。"""

import html
import re
import struct


def decode_alphabet(code):
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    if not re.fullmatch(r'[A-Za-z0-9+/_-]{1,100000}', code):
        raise ValueError('组件编码字符无效')
    code = code.replace('-', '+').replace('_', '/')
    value, bits, out = 0, 0, bytearray()
    for char in code:
        value |= alphabet.index(char) << bits
        bits += 6
        while bits >= 8:
            out.append(value & 255); value >>= 8; bits -= 8
    if bits and value:
        out.append(value)
    return bytes(out)


def snappy(data):
    pos, length, shift = 0, 0, 0
    while True:
        if pos >= len(data) or shift > 28:
            raise ValueError('压缩长度无效')
        byte = data[pos]; pos += 1
        length |= (byte & 127) << shift
        if byte < 128:
            break
        shift += 7
    if length > 2000000:
        raise ValueError('组件解压超过 2 MB')
    out = bytearray()
    def read(count):
        nonlocal pos
        if pos + count > len(data):
            raise ValueError('压缩数据截断')
        value = data[pos:pos + count]; pos += count
        return value
    while len(out) < length:
        tag = read(1)[0]; kind = tag & 3
        if kind == 0:
            size = tag >> 2
            if size >= 60:
                size = int.from_bytes(read(size - 59), 'little')
            out.extend(read(size + 1))
        else:
            if kind == 1:
                size = 4 + ((tag >> 2) & 7)
                offset = ((tag & 224) << 3) | read(1)[0]
            else:
                size = 1 + (tag >> 2)
                offset = int.from_bytes(read(2 if kind == 2 else 4), 'little')
            if not 0 < offset <= len(out):
                raise ValueError('压缩回溯偏移无效')
            for _ in range(size):
                out.append(out[-offset])
        if len(out) > length:
            raise ValueError('解压长度不匹配')
    return bytes(out)


class Reader:
    def __init__(self, code):
        self.data = decode_alphabet(code)
        self.pos = 0
        self.compressed = False
        self.version = self.read(8)
        if not 1 <= self.version <= 9:
            raise ValueError('不支持的来源组件协议版本')

    def decompress(self):
        self.align()
        self.data = snappy(self.data[self.pos // 8:]); self.pos = 0; self.compressed = True

    def read(self, count):
        if self.compressed:
            count = (count + 7) & ~7
        result = 0
        for index in range(count):
            pos = self.pos + index
            if pos // 8 < len(self.data):
                result |= ((self.data[pos // 8] >> (pos % 8)) & 1) << index
        self.pos += count
        if self.pos > len(self.data) * 8 + 1024:
            raise ValueError('组件结构超出数据边界')
        return result

    def align(self):
        self.pos = (self.pos + 7) & ~7

    def varint(self):
        result = 0
        for shift in range(0, 35, 7):
            byte = self.read(8); result |= (byte & 127) << shift
            if byte < 128:
                return result
        raise ValueError('可变整数过长')

    def string(self):
        length = self.read(8); self.align()
        return bytes(self.read(8) for _ in range(length)).decode('utf-8')

    def context(self):
        flags, out = self.read(16), {}
        for bit, key in [(1, 'spells'), (2, 'auras')]:
            if flags & bit:
                count = self.varint()
                if count > 10000:
                    raise ValueError('上下文数组过长')
                out[key] = [self.read(24) for _ in range(count)]
        if flags & 4 and not flags & 2:
            out['auras'] = out.get('spells', [])
        if flags & 8:
            count = self.varint()
            if count > 10000:
                raise ValueError('天赋上下文过长')
            out['traitEntryRank'] = {self.read(24): self.read(8) for _ in range(count)}
        for bit, key, width in [(16, 'difficulty', 8), (32, 'itemLevel', 16), (64, 'playerClass', 8),
                (128, 'playerLevel', 8), (256, 'playerGender', 8), (512, 'specializationIndex', 8)]:
            if flags & bit:
                out[key] = self.read(width)
        if self.version >= 8 and flags & 1024:
            count = self.varint()
            if count > 10000:
                raise ValueError('角色上下文过长')
            out['charData'] = {self.varint(): self.varint() for _ in range(count)}
        return out

    def item(self, object_id=None):
        out = {'id': self.read(20) if object_id is None else object_id}
        flags = self.read(32 if self.version >= 5 else 16) if self.version >= 3 else 0
        def flag(count):
            nonlocal flags
            if self.version < 3:
                return self.read(count)
            value = flags & ((1 << count) - 1); flags >>= count
            return value
        if flag(1):
            out['specialization'] = self.read(16)
        for key, width in [('modItems', 20), ('modSpells', 24 if self.version >= 9 else 20), ('gems', 20)]:
            out[key] = [self.read(width) for _ in range(flag(3))]
        if self.version >= 2:
            if flag(1):
                out['level'] = self.read(8)
            out['bonuses'] = [self.read(16) for _ in range(flag(4))]
        if self.version >= 3 and flag(1):
            out['context'] = self.read(8)
        if self.version >= 5 and flag(1):
            out['itemLevel'] = self.read(16)
        if self.version >= 8 and flag(1):
            out['customContext'] = self.context()
        if self.version >= 9 and flag(1):
            out['inheritStats'] = self.read(20)
        return out

    def reference(self):
        if self.version < 7:
            object_id = self.read(24)
            return {'type': 'spell', 'id': object_id & 0x7fffff} if object_id & 0x800000 else {'type': 'item', 'item': {'id': object_id}}
        flags = self.read(8)
        name = self.string() if flags & 128 else ''
        kind = flags & 63
        if kind == 1:
            return {'type': 'item', 'item': self.item(), 'name': name}
        if kind == 2:
            result = {'type': 'spell', 'id': self.read(24), 'name': name}
            if flags & 64:
                result['context'] = self.context()
            return result
        if kind in (3, 4, 5, 6, 7):
            return {'type': {3: 'race', 4: 'class', 5: 'spec', 6: 'currency', 7: 'keystoneAffix'}[kind], 'id': self.read(24), 'name': name}
        return {'type': 'spell', 'id': 0, 'name': name}

    def simulation_reference(self):
        if self.version >= 7:
            ref = self.reference()
            return None if ref.get('type') == 'spell' and not ref.get('id') else ref
        if self.version >= 6:
            flags = self.read(8); kind = flags & 15
            if kind == 1:
                ref = {'type': 'item', 'item': self.item()}
            elif kind in (2, 3):
                ref = {'type': 'spell' if kind == 2 else 'race', 'id': self.read(24 if kind == 2 else 8)}
            else:
                return None
            if flags & 16:
                ref['name'] = self.string()
            return ref
        object_id = self.read(24)
        if not object_id:
            return None
        if object_id & 0x800000:
            return {'type': 'race', 'id': object_id & 0x3fffff} if object_id & 0x400000 else {'type': 'item', 'item': self.item(object_id & 0x3fffff)}
        return {'type': 'spell', 'id': object_id & 0x7fffff}


def decode_component(kind, code):
    r = Reader(code)
    if kind in ('paperdoll', 'simulation'):
        if r.version >= 3:
            r.decompress()
    else:
        r.decompress()
    if kind == 'priority':
        count = r.read(8)
        return {'stats': [r.read(8) for _ in range(count)], 'relation': [r.read(8) for _ in range(max(0, count - 1))]}
    if kind == 'paperdoll':
        specialization = r.read(16) if r.version >= 4 else 71
        mask, items = r.read(19), {}
        for index in range(19):
            if mask & (1 << index):
                items[str(index)] = r.reference() if r.version >= 7 else {'type': 'item', 'item': r.item()}
        return {'specialization': specialization, 'items': items}
    if kind == 'rotation':
        def entries(depth=0):
            if depth > 16:
                raise ValueError('循环分支过深')
            rows = []
            for _ in range(r.read(8)):
                entry = r.reference(); entry['label'] = r.string(); flags = r.read(8)
                entry['extra'] = [r.reference() for _ in range(flags & 63)]
                if flags & 128:
                    entry['branch'] = entries(depth + 1)
                entry['rejoin'] = bool(flags & 64); rows.append(entry)
            return rows
        return {'entries': entries()}
    if kind == 'timeline':
        out = {'color': r.read(24), 'duration': r.read(16)}
        if r.version >= 2:
            flags = r.read(8); out.update(stackUpper=flags & 15, stackLower=(flags >> 4) & 15)
        else:
            out.update(stackUpper=2 if r.read(1) else 1, stackLower=0)
        out['segments'] = [{'color': r.read(24), 'start': r.read(16), 'end': r.read(16)} for _ in range(r.read(8))]
        for key in ('upper', 'lower'):
            out[key] = [{**r.reference(), 'time': r.read(16)} for _ in range(r.read(8))]
        out['flags'] = r.read(8) if r.version >= 3 else 0
        return out
    if kind == 'simulation':
        rows = []
        for _ in range(r.read(8)):
            a, b = r.simulation_reference(), r.simulation_reference() if r.version >= 6 else None
            value = struct.unpack('<f', bytes(r.read(8) for _ in range(4)))[0]
            if a:
                rows.append({'item': a, 'item2': b, 'value': value})
        return {'lines': rows, 'flags': r.read(8) if r.version >= 6 else 1, 'selected': r.read(8) if r.version >= 6 else None}
    raise ValueError('不支持的组件类型')


SLOTS = ['头部', '项链', '肩部', '衬衣', '胸部', '腰部', '腿部', '脚部', '腕部', '手部', '戒指一', '戒指二', '饰品一', '饰品二', '背部', '主手', '副手', '远程', '战袍']
STATS = {3: '敏捷', 4: '力量', 5: '智力', 7: '耐力', 32: '暴击', 36: '急速', 40: '全能', 49: '精通', 61: '速度', 62: '吸血', 63: '闪避', 71: '主属性'}


def ref_text(ref):
    if not ref:
        return ''
    kind = ref.get('type')
    if kind == 'item':
        return '[[item:{}]]'.format(ref['item']['id'])
    if kind == 'spell':
        if not ref.get('id'):
            return html.escape(ref.get('name', ''))
        return '[[spell:{}]]'.format(ref['id'])
    return html.escape(ref.get('name') or '{} {}'.format({'race': '种族', 'class': '职业', 'spec': '专精', 'currency': '货币', 'keystoneAffix': '词缀'}.get(kind, kind), ref.get('id', '')))


def component_html(kind, data):
    if kind == 'priority':
        relations = {0: ' ', 1: ' ＞ ', 2: ' ＝ ', 3: ' ≥ ', 4: ' ≫ '}
        parts = []
        for i, stat in enumerate(data['stats']):
            if i:
                parts.append(relations.get(data['relation'][i - 1], ' ＞ '))
            parts.append(STATS.get(stat, '属性 {}'.format(stat)))
        return '<p><strong>' + ''.join(parts) + '</strong></p>'
    if kind == 'paperdoll':
        rows = []
        for slot, ref in data['items'].items():
            item = ref.get('item', {})
            extras = ['[[item:{}]]'.format(i) for i in item.get('gems', []) + item.get('modItems', [])]
            extras += ['[[spell:{}]]'.format(i) for i in item.get('modSpells', [])]
            rows.append('<tr><th>{}</th><td>{}</td><td>{}</td></tr>'.format(SLOTS[int(slot)], ref_text(ref), ' · '.join(extras)))
        return '<table><thead><tr><th>部位</th><th>推荐装备</th><th>宝石与强化</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'
    if kind == 'rotation':
        def entries(rows):
            return '<ol>' + ''.join('<li>{} {} {}{}{}</li>'.format(ref_text(r), html.escape(r.get('label', '')),
                ' · '.join(ref_text(x) for x in r.get('extra', [])), entries(r['branch']) if r.get('branch') else '',
                '<p>随后回到主循环</p>' if r.get('rejoin') else '') for r in rows) + '</ol>'
        return entries(data['entries'])
    if kind == 'timeline':
        rows = [(r.get('time', 0), ref_text(r), label) for key, label in [('upper', '上轨'), ('lower', '下轨')] for r in data[key]]
        return '<p>总时长：{} 秒</p><table><thead><tr><th>时间</th><th>动作</th><th>轨道</th></tr></thead><tbody>'.format(data['duration']) + ''.join('<tr><td>{} 秒</td><td>{}</td><td>{}</td></tr>'.format(*r) for r in sorted(rows)) + '</tbody></table>'
    if kind == 'simulation':
        return '<table><thead><tr><th>方案</th><th>模拟数值</th></tr></thead><tbody>' + ''.join('<tr><td>{} {}</td><td>{:,.2f}</td></tr>'.format(ref_text(r['item']), ref_text(r.get('item2')), r['value']) for r in data['lines']) + '</tbody></table>'
    return ''
