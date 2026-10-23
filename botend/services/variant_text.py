"""无损文本模板：公共结构只存一次，变体保存原始数值及其类型。"""
from copy import deepcopy
import hashlib
import json
import re

NUMBER = re.compile(r'\d[\d,]*(?:\.\d+)?')
MARKER = '$variant_text'


def byte_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def encode(value, templates):
    values = []
    def parameter(value):
        values.append(value)
        return ['v', len(values) - 1]
    def visit(value):
        if isinstance(value, dict):
            return ['d', [[key, visit(row)] for key, row in sorted(value.items())]]
        if isinstance(value, list):
            return ['l', [visit(row) for row in value]]
        if isinstance(value, str):
            parts, last = [], 0
            for match in NUMBER.finditer(value):
                parts.extend([value[last:match.start()], parameter(match[0])[1]])
                last = match.end()
            return ['s', parts + [value[last:]]] if parts else ['c', value]
        if type(value) in (int, float):
            return parameter(value)
        if value is None or type(value) is bool:
            return ['c', value]
        raise ValueError('不支持的变体文本类型')
    template = visit(value)
    key = digest(template)
    templates[key] = template
    return {MARKER: 1, 'template': key, 'values': values}


def is_reference(value):
    return isinstance(value, dict) and value.get(MARKER) == 1


def decode(reference, templates):
    if isinstance(reference, dict) and MARKER in reference and not is_reference(reference):
        raise ValueError('不支持的变体文本版本')
    if not is_reference(reference):
        return deepcopy(reference)
    template = templates.get(reference.get('template'))
    if template is None or digest(template) != reference.get('template'):
        raise ValueError('变体文本模板缺失或校验失败')
    values = reference.get('values')
    if not isinstance(values, list):
        raise ValueError('变体文本参数无效')
    used = set()
    def parameter(index):
        if type(index) is not int or not 0 <= index < len(values):
            raise ValueError('变体文本参数索引无效')
        used.add(index)
        return deepcopy(values[index])
    def visit(node):
        kind, data = node
        if kind == 'c':
            return deepcopy(data)
        if kind == 'v':
            return parameter(data)
        if kind == 's':
            return ''.join(str(parameter(part)) if type(part) is int else part for part in data)
        if kind == 'l':
            return [visit(row) for row in data]
        if kind == 'd':
            return {key: visit(row) for key, row in data}
        raise ValueError('变体文本模板结构无效')
    result = visit(template)
    if used != set(range(len(values))):
        raise ValueError('变体文本参数数量不匹配')
    return result


def stored_effects(variant):
    raw = variant.effects_json
    return decode(raw, variant.item.variant_text_templates if is_reference(raw) else {})


def prepare_effects(item, raw):
    """采集事务内复用已有模板；新文本交由批量同步核算共享收益。"""
    if is_reference(raw):
        decode(raw, item.variant_text_templates)
        return raw
    if not raw:
        return raw
    from django.db import transaction
    from botend.models import WowItemSnapshot
    with transaction.atomic():
        locked = WowItemSnapshot.objects.select_for_update().only('pk', 'variant_text_templates').get(pk=item.pk)
        templates = deepcopy(locked.variant_text_templates)
        result = encode(raw, templates)
        if decode(result, templates) != raw:
            raise ValueError('变体文本无法无损转换')
        # 新模板在批量同步时评估共享收益；短文本不强行换成长引用。
        if (result['template'] not in locked.variant_text_templates
                or byte_size(result) >= byte_size(raw)):
            return raw
        item.variant_text_templates = templates
        return result


def compact_item(item_id, *, apply=False, restore=False):
    """按单物品事务转换全部批次；只保留实际变体引用的模板。"""
    from django.db import transaction
    from botend.models import WowItemSnapshot, WowItemVariantSnapshot
    with transaction.atomic():
        item = WowItemSnapshot.objects.select_for_update().only('pk', 'variant_text_templates').get(pk=item_id)
        rows = list(WowItemVariantSnapshot.objects.select_for_update().filter(item=item)
                    .only('pk', 'effects_json').order_by('pk'))
        templates, changed, candidates = {}, [], []
        before = byte_size(item.variant_text_templates)
        after = 0
        for row in rows:
            raw = decode(row.effects_json, item.variant_text_templates)
            packed = encode(raw, templates) if raw else raw
            if decode(packed, templates) != raw:
                raise ValueError('变体原文回读不一致')
            candidates.append((row, raw, packed))
        # 单独一条描述可能比模板结构更小；按模板组核算总量，避免负优化。
        savings = {key: -byte_size({key: template}) for key, template in templates.items()}
        for row, raw, packed in candidates:
            if is_reference(packed):
                savings[packed['template']] += byte_size(raw) - byte_size(packed)
        templates = {key: template for key, template in templates.items() if savings[key] > 0 and not restore}
        for row, raw, packed in candidates:
            if is_reference(packed) and packed['template'] not in templates:
                packed = raw
            before += byte_size(row.effects_json)
            after += byte_size(packed)
            if packed != row.effects_json:
                row.effects_json = packed
                changed.append(row)
        after += byte_size(templates)
        if apply:
            if templates != item.variant_text_templates:
                WowItemSnapshot.objects.filter(pk=item.pk).update(variant_text_templates=templates)
            if changed:
                WowItemVariantSnapshot.objects.bulk_update(changed, ['effects_json'], batch_size=200)
        return {'items': 1, 'variants': len(changed), 'before_bytes': before, 'after_bytes': after,
                'templates': len(templates)}


def maintain_variant_text(*, limit=200):
    """持久化扫描游标，进程重启可续跑；坏物品单独报错，不阻塞后续物品。"""
    from pathlib import Path
    from django.conf import settings
    from botend.models import WowItemSnapshot
    from .simc_benchmark_result_snapshot import _load, _write, _lock
    root = Path(getattr(settings, 'VARIANT_TEXT_MAINTENANCE_ROOT',
                        Path(settings.BASE_DIR) / 'var' / 'variant-text'))
    report = dict(kind='variant_text', apply=True, status='busy', deleted=0,
                  deleted_bytes=0, items=0, variants=0, errors=[])
    with _lock(root / 'maintenance.lock', blocking=False) as acquired:
        if not acquired:
            return report
        state = _load(root / 'cursor.json', {})
        after = int(state.get('after', 0))
        size = max(1, min(200, int(limit)))
        ids = list(WowItemSnapshot.objects.filter(pk__gt=after).order_by('pk')
                   .values_list('pk', flat=True)[:size])
        for pk in ids:
            try:
                result = compact_item(pk, apply=True)
                report['items'] += 1
                report['variants'] += result['variants']
            except Exception as exc:
                report['errors'].append({'item': pk, 'error': str(exc)})
        state = {'after': ids[-1] if len(ids) == size else 0}
        _write(root / 'cursor.json', state)
        report.update(status='error' if report['errors'] else 'ok', next_cursor=state['after'])
        if report['errors']:
            report['error'] = json.dumps(report['errors'], ensure_ascii=False)
    return report


def catalog_text(payload, *, expand=False):
    """仅遍历目录协议的变体位置，不改写任意业务 JSON 或用户历史快照。"""
    result = deepcopy(payload)
    templates = result.pop('variant_text_templates', {}) if expand else {}
    def item(row):
        for variant in row.get('variants', []):
            if expand:
                reference = variant.pop('text_reference', None)
                if reference is not None:
                    variant.update(decode(reference, templates))
            else:
                text = {key: variant.pop(key) for key in ('effects', 'tooltip', '_item') if key in variant}
                if text:
                    variant['text_reference'] = encode(text, templates)
    for row in result.get('items', []):
        item(row)
    for rows in result.get('groups', {}).values():
        for row in rows:
            item(row)
    for row in result.get('embellishment_options', {}).values():
        item(row)
    if not expand and templates:
        result['variant_text_templates'] = templates
    return result


def compact_catalog_text(payload):
    """同时比较解压后体积与 gzip 体积，仅发布确有收益的模板格式。"""
    import zlib
    def sizes(value):
        compressor = zlib.compressobj(level=6, wbits=31)
        plain = compressed = 0
        for part in json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False).iterencode(value):
            body = part.encode()
            plain += len(body)
            compressed += len(compressor.compress(body))
        return plain, compressed + len(compressor.flush())
    packed = catalog_text(payload)
    before, after = sizes(payload), sizes(packed)
    return packed if all(new < old for new, old in zip(after, before)) else payload
