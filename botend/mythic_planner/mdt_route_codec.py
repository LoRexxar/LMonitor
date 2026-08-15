"""MDT 6.2.2 Blizzard 路线字符串编解码与路线模型转换。"""

import base64
import math
import struct
import zlib

from botend.models import MythicDungeon, MythicDungeonFloor, MythicDungeonSpawn


MDT2_PREFIX = '!~MDT2~'
MAX_ROUTE_BYTES = 2 * 1024 * 1024
MAX_CBOR_DEPTH = 100
MAP_SOURCE_WIDTH = 840.0
MAP_SOURCE_HEIGHT = 560.0
DEFAULT_PULL_COLORS = (
    '#e879f9', '#2dd4bf', '#f87171', '#60a5fa', '#facc15',
    '#4ade80', '#fb7185', '#a78bfa', '#38bdf8', '#f97316',
    '#84cc16', '#ec4899', '#14b8a6', '#818cf8', '#eab308',
)


class _CBORUndefined:
    pass


CBOR_UNDEFINED = _CBORUndefined()


def _encode_argument(major, value):
    if value < 0:
        raise ValueError('CBOR 长度不能为负数。')
    prefix = major << 5
    if value < 24:
        return bytes((prefix | value,))
    if value <= 0xFF:
        return bytes((prefix | 24, value))
    if value <= 0xFFFF:
        return bytes((prefix | 25,)) + struct.pack('>H', value)
    if value <= 0xFFFFFFFF:
        return bytes((prefix | 26,)) + struct.pack('>I', value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return bytes((prefix | 27,)) + struct.pack('>Q', value)
    raise ValueError('CBOR 整数超出 64 位范围。')


def _encode_float(value):
    if math.isnan(value):
        return b'\xf9\x7e\x00'
    if value == math.inf:
        return b'\xf9\x7c\x00'
    if value == -math.inf:
        return b'\xf9\xfc\x00'
    if value == 0.0 and math.copysign(1.0, value) < 0:
        return b'\xfb' + struct.pack('>d', value)
    try:
        half = struct.pack('>e', value)
        if struct.unpack('>e', half)[0] == value:
            return b'\xf9' + half
    except (OverflowError, struct.error):
        pass
    try:
        single = struct.pack('>f', value)
        if struct.unpack('>f', single)[0] == value:
            return b'\xfa' + single
    except OverflowError:
        pass
    return b'\xfb' + struct.pack('>d', value)


def encode_blizzard_cbor(value, *, _depth=0):
    """编码暴雪 SerializeCBOR 使用的 RFC 8949 子集。

    暴雪会把 Lua string 固定编码为 major type 2，而不是 UTF-8 text。
    """

    if _depth > MAX_CBOR_DEPTH:
        raise ValueError('路线结构嵌套过深。')
    if value is None:
        return b'\xf6'
    if value is CBOR_UNDEFINED:
        return b'\xf7'
    if value is False:
        return b'\xf4'
    if value is True:
        return b'\xf5'
    if isinstance(value, int):
        if value >= 0:
            return _encode_argument(0, value)
        return _encode_argument(1, -1 - value)
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        raw = value.encode('utf-8')
        return _encode_argument(2, len(raw)) + raw
    if isinstance(value, bytes):
        return _encode_argument(2, len(value)) + value
    if isinstance(value, (list, tuple)):
        return _encode_argument(4, len(value)) + b''.join(
            encode_blizzard_cbor(item, _depth=_depth + 1)
            for item in value
        )
    if isinstance(value, dict):
        chunks = [_encode_argument(5, len(value))]
        for key, item in value.items():
            chunks.append(encode_blizzard_cbor(key, _depth=_depth + 1))
            chunks.append(encode_blizzard_cbor(item, _depth=_depth + 1))
        return b''.join(chunks)
    raise ValueError(f'路线包含无法序列化的类型：{type(value).__name__}')


class _CBORDecoder:
    def __init__(self, data):
        self.data = memoryview(data)
        self.offset = 0

    def _read(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError('CBOR 数据意外结束。')
        result = self.data[self.offset:end].tobytes()
        self.offset = end
        return result

    def _argument(self, additional):
        if additional < 24:
            return additional
        sizes = {24: 1, 25: 2, 26: 4, 27: 8}
        size = sizes.get(additional)
        if size is None:
            raise ValueError('不支持 indefinite-length CBOR 数据。')
        return int.from_bytes(self._read(size), 'big')

    @staticmethod
    def _decode_string(raw):
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('路线 CBOR 包含非 UTF-8 字符串。') from exc

    def decode(self, depth=0):
        if depth > MAX_CBOR_DEPTH:
            raise ValueError('路线结构嵌套过深。')
        initial = self._read(1)[0]
        major = initial >> 5
        additional = initial & 0x1F
        if major == 0:
            return self._argument(additional)
        if major == 1:
            return -1 - self._argument(additional)
        if major in (2, 3):
            length = self._argument(additional)
            return self._decode_string(self._read(length))
        if major == 4:
            length = self._argument(additional)
            return [self.decode(depth + 1) for _ in range(length)]
        if major == 5:
            length = self._argument(additional)
            result = {}
            for _ in range(length):
                key = self.decode(depth + 1)
                try:
                    hash(key)
                except TypeError as exc:
                    raise ValueError('路线 CBOR 使用了不支持的复合键。') from exc
                result[key] = self.decode(depth + 1)
            return result
        if major == 6:
            self._argument(additional)
            return self.decode(depth + 1)
        if major != 7:
            raise ValueError('路线 CBOR 类型不受支持。')
        if additional == 20:
            return False
        if additional == 21:
            return True
        if additional == 22:
            return None
        if additional == 23:
            return CBOR_UNDEFINED
        if additional == 24:
            self._read(1)
            return CBOR_UNDEFINED
        if additional == 25:
            return struct.unpack('>e', self._read(2))[0]
        if additional == 26:
            return struct.unpack('>f', self._read(4))[0]
        if additional == 27:
            return struct.unpack('>d', self._read(8))[0]
        raise ValueError('路线 CBOR simple value 不受支持。')


def decode_blizzard_cbor(data):
    decoder = _CBORDecoder(data)
    value = decoder.decode()
    if decoder.offset != len(decoder.data):
        raise ValueError('路线 CBOR 末尾包含多余数据。')
    return value


def encode_mdt2_table(value):
    serialized = encode_blizzard_cbor(value)
    if len(serialized) > MAX_ROUTE_BYTES:
        raise ValueError('路线数据超过 2 MB，无法导出。')
    compressor = zlib.compressobj(level=9, wbits=-15)
    compressed = compressor.compress(serialized) + compressor.flush()
    return MDT2_PREFIX + base64.b64encode(compressed).decode('ascii')


def decode_mdt2_table(code):
    text = str(code or '').strip()
    if not text.startswith(MDT2_PREFIX):
        raise ValueError('仅支持 !~MDT2~ 开头的路线字符串。')
    encoded = text[len(MDT2_PREFIX):]
    if not encoded:
        raise ValueError('路线字符串内容为空。')
    if len(encoded) > ((MAX_ROUTE_BYTES + 2) // 3) * 4 + 4:
        raise ValueError('路线字符串超过 2 MB 限制。')
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('路线字符串 Base64 无法解码。') from exc
    try:
        decompressor = zlib.decompressobj(wbits=-15)
        serialized = decompressor.decompress(compressed, MAX_ROUTE_BYTES + 1)
        if len(serialized) > MAX_ROUTE_BYTES:
            raise ValueError('路线数据超过 2 MB，无法导入。')
        serialized += decompressor.flush(MAX_ROUTE_BYTES + 1 - len(serialized))
    except zlib.error as exc:
        raise ValueError('路线字符串 Deflate 无法解压。') from exc
    if len(serialized) > MAX_ROUTE_BYTES:
        raise ValueError('路线数据超过 2 MB，无法导入。')
    if not decompressor.eof or decompressor.unused_data:
        raise ValueError('路线字符串 Deflate 数据不完整。')
    return decode_blizzard_cbor(serialized)


def _active_dungeon_by_key(key):
    return (
        MythicDungeon.objects.filter(
            key=key,
            is_active=True,
            data_version__is_active=True,
        )
        .select_related('data_version')
        .order_by('-data_version__imported_at', '-data_version_id')
        .first()
    )


def _active_dungeon_by_external_index(external_index):
    return (
        MythicDungeon.objects.filter(
            external_index=external_index,
            is_active=True,
            data_version__is_active=True,
        )
        .select_related('data_version')
        .order_by('-data_version__imported_at', '-data_version_id')
        .first()
    )


def _dungeon_maps(dungeon):
    floors = list(
        MythicDungeonFloor.objects.filter(dungeon=dungeon, is_active=True)
        .order_by('floor_index')
    )
    spawns = list(
        MythicDungeonSpawn.objects.filter(
            enemy__dungeon=dungeon,
            enemy__is_active=True,
            floor__is_active=True,
            is_active=True,
        ).select_related('enemy', 'floor')
    )
    by_uid = {}
    by_source = {}
    for spawn in spawns:
        enemy_index = (spawn.enemy.metadata or {}).get('source_enemy_index')
        clone_index = (spawn.metadata or {}).get('source_clone_index')
        if not isinstance(enemy_index, int) or not isinstance(clone_index, int):
            continue
        uid = f'{spawn.enemy.key}:{spawn.key}'
        by_uid[uid] = (enemy_index, clone_index)
        by_source[(enemy_index, clone_index)] = uid
    return floors, by_uid, by_source


def _hex_color(value, fallback):
    text = str(value or '').strip().lower()
    if text.startswith('#'):
        text = text[1:]
    if len(text) == 6 and all(char in '0123456789abcdef' for char in text):
        return text
    return fallback.lstrip('#')


def _floor_index(floor_by_key, floor_key, fallback=1):
    floor = floor_by_key.get(str(floor_key or ''))
    return int(floor.floor_index if floor else fallback)


def _mdt_xy(point):
    return (
        round(float(point.get('x', 0)) / 100.0 * MAP_SOURCE_WIDTH, 1),
        round(-float(point.get('y', 0)) / 100.0 * MAP_SOURCE_HEIGHT, 1),
    )


def _portal_xy(x, y):
    return {
        'x': round(max(0.0, min(100.0, float(x) / MAP_SOURCE_WIDTH * 100.0)), 6),
        'y': round(max(0.0, min(100.0, -float(y) / MAP_SOURCE_HEIGHT * 100.0)), 6),
    }


def _annotation_to_object(annotation, floor_by_key):
    annotation_type = str(annotation.get('type') or '')
    color = _hex_color(annotation.get('color'), '#facc15')
    sublevel = _floor_index(floor_by_key, annotation.get('floor_key'))
    if annotation_type == 'note':
        x, y = _mdt_xy(annotation)
        return {'d': [x, y, sublevel, True, str(annotation.get('text') or '')], 'n': True}
    if annotation_type not in {'line', 'arrow', 'pencil'}:
        raise ValueError(f'不支持的地图标注类型：{annotation_type or "空"}')
    points = annotation.get('points')
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('地图线条标注至少需要两个点。')
    source_points = [_mdt_xy(point) for point in points]
    coordinates = []
    for start, end in zip(source_points, source_points[1:]):
        coordinates.extend((start[0], start[1], end[0], end[1]))
    smooth = annotation_type == 'pencil'
    brush_size = 11 if annotation_type == 'arrow' else 3
    result = {
        'd': [brush_size, 1 if annotation_type == 'arrow' else 1.1, sublevel, True, color, 0, smooth],
        'l': coordinates,
    }
    if annotation_type == 'arrow':
        start, end = source_points[-2], source_points[-1]
        result['t'] = [math.atan2(start[1] - end[1], start[0] - end[0])]
    return result


def _objects_from_annotations(annotations, floor_by_key):
    objects = []
    metadata = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise ValueError('地图标注内容格式不正确。')
        objects.append(_annotation_to_object(annotation, floor_by_key))
        metadata.append({
            'id': str(annotation.get('id') or ''),
            'type': str(annotation.get('type') or ''),
            'color': str(annotation.get('color') or '#facc15'),
        })
    return objects, metadata


def _lua_sequence(value, label):
    """把 Blizzard 可能编码成 array 或整数键 map 的 Lua 表转成有序列表。"""

    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        indexed = []
        for key, item in value.items():
            if not isinstance(key, int) or key < 1:
                raise ValueError(f'{label} 包含非正整数索引。')
            indexed.append((key, item))
        return [item for _key, item in sorted(indexed)]
    raise ValueError(f'{label} 不是数组或整数键表。')


def _lua_index(value, index, default=None):
    if isinstance(value, list):
        return value[index - 1] if 0 < index <= len(value) else default
    if isinstance(value, dict):
        return value.get(index, default)
    return default


def route_payload_to_preset(payload, dungeon=None):
    dungeon = dungeon or _active_dungeon_by_key(str((payload or {}).get('dungeon_key') or ''))
    if not dungeon:
        raise ValueError('路线所属地下城不存在。')
    if dungeon.external_index is None:
        raise ValueError('地下城缺少 MDT external index，无法导出。')
    floors, by_uid, _by_source = _dungeon_maps(dungeon)
    floor_by_key = {floor.key: floor for floor in floors}
    pulls = []
    pull_metadata = []
    for index, pull in enumerate(payload.get('pulls') or [], start=1):
        mdt_pull = {}
        for uid in pull.get('spawn_uids') or []:
            source = by_uid.get(str(uid))
            if not source:
                raise ValueError(f'怪物刷新点缺少 MDT source index：{uid}')
            enemy_index, clone_index = source
            mdt_pull.setdefault(enemy_index, []).append(clone_index)
        mdt_pull['color'] = _hex_color(
            pull.get('color'),
            DEFAULT_PULL_COLORS[(index - 1) % len(DEFAULT_PULL_COLORS)],
        )
        for enemy_index, clone_indexes in list(mdt_pull.items()):
            if isinstance(enemy_index, int):
                clone_indexes.sort()
        pulls.append(mdt_pull)
        pull_metadata.append({
            'id': str(pull.get('id') or ''),
            'name': str(pull.get('name') or f'第 {index} 波'),
        })
    if not pulls:
        pulls = [{'color': DEFAULT_PULL_COLORS[0].lstrip('#')}]
        pull_metadata = [{'id': '', 'name': '第 1 波'}]
    current_floor_key = str(payload.get('current_floor_key') or '')
    current_sublevel = _floor_index(floor_by_key, current_floor_key)
    objects, annotation_metadata = _objects_from_annotations(
        payload.get('annotations') or [],
        floor_by_key,
    )
    return {
        'text': str(payload.get('name') or 'LMonitor 路线')[:160],
        'value': {
            'currentDungeonIdx': int(dungeon.external_index),
            'currentPull': 1,
            'currentSublevel': current_sublevel,
            'pulls': pulls,
        },
        'objects': objects,
        'difficulty': int(payload.get('dungeon_level') or 10),
        'lmonitor': {
            'version': 1,
            'pulls': pull_metadata,
            'annotations': annotation_metadata,
        },
    }


def _object_segments(obj):
    coordinates = obj.get('l')
    try:
        coordinates = _lua_sequence(coordinates, 'MDT 地图线条坐标')
    except ValueError:
        return []
    if len(coordinates) < 4:
        return []
    segments = []
    for index in range(0, len(coordinates) - 3, 4):
        segments.append((
            (coordinates[index], coordinates[index + 1]),
            (coordinates[index + 2], coordinates[index + 3]),
        ))
    return segments


def _segments_to_points(segments):
    points = []
    for start, end in segments:
        start_point = _portal_xy(*start)
        end_point = _portal_xy(*end)
        if not points or points[-1] != start_point:
            points.append(start_point)
        points.append(end_point)
    return points


def _object_to_annotation(obj, index, floor_by_index, metadata=None):
    if not isinstance(obj, dict):
        raise ValueError(f'MDT 第 {index} 个地图标注格式不正确。')
    details = obj.get('d')
    if not isinstance(details, (list, dict)) or _lua_index(details, 5) is None:
        raise ValueError(f'MDT 第 {index} 个地图标注缺少 d 数据。')
    sublevel = int(_lua_index(details, 3, 1) or 1)
    floor = floor_by_index.get(sublevel)
    if not floor:
        raise ValueError(f'MDT 地图标注引用了不存在的楼层：{sublevel}')
    metadata = metadata if isinstance(metadata, dict) else {}
    annotation_id = str(metadata.get('id') or f'mdt-object-{index}')
    color = '#' + _hex_color(metadata.get('color') or _lua_index(details, 5), '#facc15')
    if obj.get('n') is True:
        point = _portal_xy(_lua_index(details, 1, 0), _lua_index(details, 2, 0))
        return {
            'id': annotation_id,
            'type': 'note',
            'floor_key': floor.key,
            **point,
            'text': str(_lua_index(details, 5, '') or '')[:300],
            'color': color,
        }
    segments = _object_segments(obj)
    if not segments:
        raise ValueError(f'MDT 第 {index} 个地图线条没有有效坐标。')
    preferred_type = str(metadata.get('type') or '')
    if obj.get('t'):
        annotation_type = 'arrow'
    elif preferred_type in {'line', 'pencil'}:
        annotation_type = preferred_type
    else:
        annotation_type = 'line' if len(segments) == 1 else 'pencil'
    return {
        'id': annotation_id,
        'type': annotation_type,
        'floor_key': floor.key,
        'points': _segments_to_points(segments),
        'color': color,
    }


def preset_to_route_payload(preset):
    if not isinstance(preset, dict):
        raise ValueError('MDT 路线内容不是 preset 表。')
    value = preset.get('value')
    if not isinstance(value, dict):
        raise ValueError('MDT 路线缺少 value 数据。')
    dungeon_index = value.get('currentDungeonIdx')
    if not isinstance(dungeon_index, int):
        raise ValueError('MDT 路线缺少地下城索引。')
    dungeon = _active_dungeon_by_external_index(dungeon_index)
    if not dungeon:
        raise ValueError(f'当前数据版本不存在 MDT 地下城索引：{dungeon_index}')
    floors, _by_uid, by_source = _dungeon_maps(dungeon)
    floor_by_index = {floor.floor_index: floor for floor in floors}
    lmonitor = preset.get('lmonitor') if isinstance(preset.get('lmonitor'), dict) else {}
    pull_metadata = lmonitor.get('pulls') if isinstance(lmonitor.get('pulls'), list) else []
    pulls = []
    raw_pulls = _lua_sequence(value.get('pulls'), 'MDT 路线的 pulls')
    seen_uids = set()
    for index, mdt_pull in enumerate(raw_pulls, start=1):
        if isinstance(mdt_pull, list) and not mdt_pull:
            mdt_pull = {}
        if not isinstance(mdt_pull, dict):
            raise ValueError(f'MDT 第 {index} 波格式不正确。')
        spawn_uids = []
        for enemy_index, clone_indexes in mdt_pull.items():
            if enemy_index == 'color':
                continue
            if not isinstance(enemy_index, int) or not isinstance(clone_indexes, list):
                continue
            for clone_index in clone_indexes:
                if not isinstance(clone_index, int):
                    raise ValueError(f'MDT 第 {index} 波包含无效 clone index。')
                uid = by_source.get((enemy_index, clone_index))
                if not uid:
                    raise ValueError(
                        f'MDT 第 {index} 波包含不存在的怪物点位：{enemy_index}:{clone_index}'
                    )
                if uid in seen_uids:
                    raise ValueError(f'MDT 怪物点位被重复加入多个波次：{uid}')
                seen_uids.add(uid)
                spawn_uids.append(uid)
        metadata = pull_metadata[index - 1] if index <= len(pull_metadata) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        pulls.append({
            'id': str(metadata.get('id') or f'mdt-pull-{index}'),
            'name': str(metadata.get('name') or f'第 {index} 波')[:80],
            'color': '#' + _hex_color(
                mdt_pull.get('color'),
                DEFAULT_PULL_COLORS[(index - 1) % len(DEFAULT_PULL_COLORS)],
            ),
            'spawn_uids': spawn_uids,
        })
    if not pulls:
        pulls = [{
            'id': 'mdt-pull-1',
            'name': '第 1 波',
            'color': DEFAULT_PULL_COLORS[0],
            'spawn_uids': [],
        }]
    annotation_metadata = (
        lmonitor.get('annotations')
        if isinstance(lmonitor.get('annotations'), list)
        else []
    )
    annotations = []
    objects = _lua_sequence(preset.get('objects') or [], 'MDT 路线的 objects')
    for index, obj in enumerate(objects, start=1):
        details = obj.get('d') if isinstance(obj, dict) else None
        if _lua_index(details, 4, True) is False:
            continue
        metadata = annotation_metadata[index - 1] if index <= len(annotation_metadata) else None
        annotations.append(_object_to_annotation(obj, index, floor_by_index, metadata))
    current_sublevel = int(value.get('currentSublevel') or 1)
    current_floor = floor_by_index.get(current_sublevel) or (floors[0] if floors else None)
    return {
        'version': 1,
        'dungeon_key': dungeon.key,
        'data_version_key': dungeon.data_version.key,
        'name': str(preset.get('text') or 'MDT 导入路线')[:160],
        'dungeon_level': max(2, min(99, int(preset.get('difficulty') or 10))),
        'current_floor_key': current_floor.key if current_floor else '',
        'pulls': pulls,
        'annotations': annotations,
    }


def encode_route_payload(payload):
    return encode_mdt2_table(route_payload_to_preset(payload))


def decode_route_payload(code):
    return preset_to_route_payload(decode_mdt2_table(code))
