"""真实楼层底图采集：以楼层 ID、挑战模式 ID 和坐标边界共同校验。"""
from io import BytesIO
import hashlib
import json
import re

from PIL import Image

from botend.services.rio_statistics_localization import name_overrides

# 来源：Keystone.Guru lang/zh_CN/dungeons.php；中央名称资料优先覆盖。
FLOOR_ZH = {
    'sacrificial_approach': '祭血通道', 'mutation_chambers': '突变温室', 'altar_of_fangs': '毒牙祭坛',
    'dreamers_passage': '沉睡者之路', 'the_foraging': '觅食溪谷', 'the_heart_of_rage': '狂怒之心',
    'den_of_nalorakk': '纳洛拉克的洞穴', 'kings_rest': '诸王之眠', 'murder_row': '密谋小径',
    'the_illicit_rain': '非法降雨', 'augurs_terrace': '占卜师平台', 'infusion_chambers': '注能室',
    'ruby_overlook': '红玉瞭望台', 'ruby_life_pools': '红玉新生法池',
    'temple_of_sethraliss': '塞塔里斯神庙', 'atrium_of_sethraliss': '塞塔里斯中庭',
    'the_blinding_vale': '夺目谷', 'halls_of_spite': '怨恨大厅', 'voidscar_arena': '虚空之痕竞技场',
    'domanaars_ascent': '多曼纳尔高地',
}


def script_json(text):
    """只解析 JSON 数据，绝不执行下载的 JavaScript。"""
    start = text.index('=') + 1
    return json.JSONDecoder().raw_decode(text[start:].lstrip())[0]


def floor_metadata(html, mapping, challenge_mode_id, rio_floors):
    found = re.search(r'mapContextMappingVersionData\s*,\s*(\{)', html)
    if not found:
        raise ValueError('来源缺少楼层清单')
    inline = json.JSONDecoder().raw_decode(html[found.start(1):])[0]
    dungeon = mapping.get('dungeon', {})
    if dungeon.get('challenge_mode_id') != challenge_mode_id:
        raise ValueError('底图副本与统计副本不匹配')
    expansion, key = dungeon.get('expansion', {}).get('key', ''), dungeon.get('key', '')
    if not all(re.fullmatch(r'[a-z0-9_]+', v) for v in (expansion, key)):
        raise ValueError('底图路径无效')
    known = {str(f['id']): f for f in rio_floors}
    result = {}
    for row in inline.get('visibleFloors', []):
        identity = str(row.get('id'))
        if row.get('facade') or identity not in known:
            continue
        bounds = [row.get(f'ingame_{axis}') for axis in ('min_x', 'min_y', 'max_x', 'max_y')]
        expected = [known[identity][axis] for axis in ('ingameMinX', 'ingameMinY', 'ingameMaxX', 'ingameMaxY')]
        if any(type(a) not in (int, float) or abs(a - b) > .02 for a, b in zip(bounds, expected)):
            continue
        index = row.get('index')
        if type(index) is not int or not 1 <= index <= 100:
            continue
        leaf = str(row.get('name', '')).split('.')[-1]
        result[identity] = {'id': identity, 'floor_index': index, 'bounds': bounds,
                            'name': FLOOR_ZH.get(leaf, f'第 {index} 层'),
                            'tile_base': f'https://assets.keystone.guru/tiles_webp/{expansion}/{key}/{index}',
                            'ui_map_id': known[identity].get('uiMapId')}
    if not result:
        raise ValueError('未找到坐标一致的独立楼层底图')
    return result


def stitch_map(floor, get_bytes, destination):
    """按来源 384×256 瓦片、2 级缩放精确拼接，不重画或修改地图内容。"""
    image = Image.new('RGB', (1536, 1024))
    tiles = destination.parent / 'tiles' / destination.stem
    tiles.mkdir(parents=True, exist_ok=True)
    for y in range(4):
        for x in range(4):
            cached = tiles / f'{x}_{y}.tile'
            raw = cached.read_bytes() if cached.is_file() else None
            if raw is None:
                raw = get_bytes(f"{floor['tile_base']}/2/{x}_{y}.webp")
            with Image.open(BytesIO(raw)) as tile:
                if tile.size != (384, 256):
                    raise ValueError('来源地图瓦片尺寸不匹配')
                image.paste(tile.convert('RGB'), (x * 384, y * 256))
            if not cached.is_file():
                cached.write_bytes(raw)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix('.next')
    image.save(staging, format='WEBP', quality=92)
    staging.replace(destination)


def collect_maps(meta, scope, get_bytes, root, *, checkpoint=None):
    maps = meta.setdefault('maps', {})
    season = next(s for s in meta['seasons'] if s['slug'] == scope['rio_season'])
    for dungeon in season['dungeons']:
        cached = maps.get(dungeon['slug'], {})
        if (cached.get('scope_token') == scope['token'] and cached.get('floors')
                and all((root / 'maps' / (f['image_key'] + '.webp')).is_file() for f in cached['floors'].values())):
            continue
        slug = dungeon['slug']
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            raise ValueError('副本路径无效')
        url = f'https://keystone.guru/heatmap/retail/{slug}/embed/1?mapFacadeStyle=split_floors'
        html = get_bytes(url).decode('utf-8')
        links = re.findall(r'<script[^>]+src="([^"]+)"', html)
        source = next((u for u in links if re.fullmatch(
            rf'https://assets\.keystone\.guru/compiled/[A-Za-z0-9._-]+/mapcontext/data/{re.escape(slug)}/[0-9]+/split_floors\.js', u)), None)
        if not source:
            raise ValueError('来源未提供独立楼层映射')
        mapping = script_json(get_bytes(source).decode('utf-8'))
        floors = floor_metadata(html, mapping, dungeon['challenge_mode_id'], meta['floors'].get(str(dungeon['challenge_mode_id']), []))
        aliases = name_overrides(scope, {str(f.get('name', '')) for f in meta['floors'].get(str(dungeon['challenge_mode_id']), [])})
        for identity, floor in floors.items():
            original = next((f for f in meta['floors'][str(dungeon['challenge_mode_id'])] if str(f['id']) == identity), {})
            if aliases.get(original.get('name')):
                floor['name'] = aliases[original['name']]
            material = [scope['game_build'], source, identity, floor['bounds'], floor['tile_base']]
            image_key = hashlib.sha256(json.dumps(material).encode()).hexdigest()
            destination = root / 'maps' / (image_key + '.webp')
            if not destination.is_file():
                stitch_map(floor, get_bytes, destination)
            floor['image_key'] = image_key
            floor['image_url'] = f'/portal/api/rio-statistics/maps/{image_key}/'
        maps[slug] = {'scope_token': scope['token'], 'floors': floors, 'source': url, 'mapping_source': source}
        if checkpoint:
            checkpoint(meta)
    return maps
