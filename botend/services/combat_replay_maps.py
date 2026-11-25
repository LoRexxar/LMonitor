"""以日志 UiMapID 查楼层，bounds 按世界坐标 xmin、ymin、xmax、ymax 排列。

MDT 的六个系数将单位楼层平面映到单位纹理平面，顺序与 Canvas DOMMatrix 相同。
clip 使用同一纹理平面的坐标。客户端原图直接使用单位变换。
"""

import json
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, partial
from io import BytesIO
from pathlib import Path
from threading import Lock

import requests
from django.conf import settings
from django.core.cache.backends.filebased import FileBasedCache
from django.templatetags.static import static
from django.urls import reverse
from PIL import Image


@lru_cache(maxsize=1)
def catalog():
    path = Path(__file__).resolve().parents[1] / "data/combat_replay/floor_maps.json"
    return json.loads(path.read_text(encoding="utf-8"))


def attach_floor_assets(maps, instance_id=None):
    index = catalog()
    for observed in maps:
        floor = index["floors"].get(str(observed["id"]))
        if not floor or (instance_id is not None and instance_id not in floor["instances"]):
            continue
        x0, y0, x1, y1 = floor["bounds"]
        if observed["x1"] < x0 or observed["x0"] > x1 or observed["y1"] < y0 or observed["y0"] > y1:
            continue
        art = floor["art"]
        native_url = (
            static(f"dashboard/maps/{index['build']}/{art}.webp")
            if floor.get("bundled") else reverse("combat_replay_map", args=[index["build"], art])
        )
        terrain = {"bounds": floor["bounds"], "name": floor["name"]}
        mdt = floor.get("mdt")
        if mdt:
            prefix = f"portal/mythic_planner/vendor/mdt-{index['mdtVersion']}/maps"
            terrain.update(
                url=static(f"{prefix}/{mdt['slug']}/floor-1.webp"), source="MDT",
                matrix=mdt["matrix"], clip=mdt.get("clip", []), nativeUrl=native_url,
            )
        else:
            terrain.update(url=native_url, source="客户端", matrix=[1, 0, 0, 1, 0, 0], clip=[])
        observed["terrain"] = terrain


def read_tile(file_id, build):
    # 断网时保留已经读取的切片，用户重试整层时只补缺少的部分。
    cache = FileBasedCache(Path(settings.BASE_DIR) / ".cache/combat-replay-tiles", {"OPTIONS": {"MAX_ENTRIES": 240}})
    key = f"tile:{build}:{file_id}"
    content = cache.get(key)
    if content is None:
        response = requests.get(f"https://wago.tools/api/casc/{file_id}", params={"version": build}, timeout=(4, 12))
        response.raise_for_status()
        content = response.content
        if not content.startswith((b"BLP1", b"BLP2")) or len(content) > 10 * 1024 * 1024:
            raise ValueError("楼层切片格式异常。")
        with Image.open(BytesIO(content)) as tile:
            image = tile.convert("RGBA")
        cache.set(key, content, timeout=3600)
        return image
    with Image.open(BytesIO(content)) as tile:
        return tile.convert("RGBA")


def compose(layout, read):
    """保留透明边缘，在游戏定义的整层尺寸处裁掉右侧、底部的切片补齐区。"""
    width, height, tile_width, tile_height, ids = layout
    columns = (width + tile_width - 1) // tile_width
    canvas = Image.new("RGBA", (width, height))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for idx, tile in enumerate(pool.map(read, ids)):
            if tile.size != (tile_width, tile_height):
                raise ValueError("楼层切片尺寸与客户端地图定义不一致。")
            canvas.paste(tile, (idx % columns * tile_width, idx // columns * tile_height))
    out = BytesIO()
    canvas.save(out, "WEBP", quality=88, method=4)
    return out.getvalue()


_locks = [Lock() for _ in range(8)]


def native_art(build, art):
    index = catalog()
    if build != index["build"] or str(art) not in index["art"]:
        raise LookupError
    cache = FileBasedCache(Path(settings.BASE_DIR) / ".cache/combat-replay-maps", {"OPTIONS": {"MAX_ENTRIES": 800}})
    key = f"floor:{build}:{art}"
    with _locks[art % len(_locks)]:
        content = cache.get(key)
        if content is None:
            content = compose(index["art"][str(art)], partial(read_tile, build=build))
            cache.set(key, content, timeout=None)
    return content
