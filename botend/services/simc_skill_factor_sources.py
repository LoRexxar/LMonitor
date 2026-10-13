"""校验并投影原生乘数来源，不从累计倍率反推独立加成。"""
import copy
import math
import re


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def factor_source_layers(component_name):
    """当前协议要求覆盖的九个实际伤害乘区。"""
    return ['da_multiplier' if component_name == 'direct' else 'ta_multiplier',
            'aoe_multiplier', 'player_multiplier', 'versus_multiplier',
            'persistent_multiplier',
            'target_da_multiplier' if component_name == 'direct' else 'target_ta_multiplier',
            'versatility', 'pet_multiplier', 'target_pet_multiplier']


def validate_factor_sources(component, component_name, *, require_complete=False):
    """来源必须对应实际乘区；允许未命名原生运算，禁止不闭合的来源链。"""
    rows = component.get('runtime_factor_sources')
    if not isinstance(rows, list):
        raise ValueError('exporter 缺少原生乘数来源。')
    expected = factor_source_layers(component_name)
    # 协议 24 的旧快照仅承诺动作乘区；新导出必须显式覆盖所有乘区。
    layers = component.get('runtime_factor_source_layers', [expected[0]])
    if (not isinstance(layers, list) or not layers
            or any(not isinstance(layer, str) or layer not in expected for layer in layers)
            or len(layers) != len(set(layers)) or expected[0] not in layers
            or (require_complete and set(layers) != set(expected))):
        raise ValueError('exporter 原生乘数来源的乘区覆盖声明无效。')
    for row in rows:
        if (not isinstance(row, dict)
                or row.get('layer') not in layers
                or row.get('evidence') != 'native_multiplier_dataflow_v1'
                or not _number(row.get('factor')) or row['factor'] <= 0
                or type(row.get('source_spell_id')) is not int or row['source_spell_id'] < 0
                or type(row.get('effect_index')) is not int or row['effect_index'] < 0
                or not isinstance(row.get('source_name'), str)
                or (row['source_spell_id'] > 0 and (row['effect_index'] < 1 or not row['source_name']))
                or (row['source_spell_id'] == 0 and row['effect_index'] != 0)):
            raise ValueError('exporter 原生乘数来源结构无效。')
        if require_complete or 'source_kind' in row:
            if row.get('source_kind') != ('spell_effect' if row['source_spell_id'] else 'native_operation'):
                raise ValueError('exporter 原生乘数来源身份类型无效。')
            if not row['source_spell_id'] and (
                    not isinstance(row.get('source_file'), str)
                    or not re.fullmatch(r'engine/(?:[A-Za-z_0-9-]+/)*[A-Za-z_0-9-]+\.(?:hpp|cpp)', row['source_file'])
                    or type(row.get('source_line')) is not int or row['source_line'] <= 0):
                raise ValueError('exporter 手写乘数缺少可核对的原生源码位置。')
    for layer in layers:
        actual = (component.get('runtime_layers') or {}).get(layer)
        product = math.prod(r['factor'] for r in rows if r['layer'] == layer)
        if not _number(actual) or not math.isclose(product, actual, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f'exporter 原生乘数来源与实际乘区不一致：{layer}。')
    if require_complete and not math.isclose(component['runtime_layers']['versatility'], 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError('exporter 全能未按固定标尺归一化为零。')
    return rows


def formula_factor_rows(component, component_name):
    """有原生轨迹时按实际来源展开，历史快照继续使用原来的乘区。"""
    layers = component.get('runtime_layers') or {}
    legacy = component.get('runtime_factor_layers')
    if 'runtime_factor_sources' not in component:
        return copy.deepcopy(legacy) if isinstance(legacy, list) else [
            {'layer': key, 'factor': value} for key, value in layers.items()
        ]
    sources = validate_factor_sources(component, component_name)
    traced_layers = component.get('runtime_factor_source_layers', [factor_source_layers(component_name)[0]])
    rows = []
    inserted = set()
    base_rows = legacy if isinstance(legacy, list) else [
        {'layer': key, 'factor': value} for key, value in layers.items()
    ]
    for row in base_rows:
        layer = row.get('layer')
        if layer in traced_layers:
            if layer not in inserted:
                rows.extend(copy.deepcopy([source for source in sources if source['layer'] == layer]))
                inserted.add(layer)
        elif _number(row.get('factor')):
            rows.append(copy.deepcopy(row))
    rows.extend(copy.deepcopy([source for source in sources if source['layer'] not in inserted]))
    return rows


def validate_excluded_sources(actor):
    """已声明排除的效果不得再次出现在实际乘法数据流中。"""
    excluded = {(r.get('spell_id'), r.get('effect_index'))
                for r in actor.get('normalized_scope_effects') or [] if isinstance(r, dict)}
    for action in actor.get('actions') or []:
        if not isinstance(action, dict):
            continue
        amounts = [action.get('baseline'), *(s.get('values') for s in action.get('scenarios') or []
                                             if isinstance(s, dict))]
        for amount in amounts:
            if not isinstance(amount, dict):
                continue
            for kind in ('direct', 'tick'):
                component = amount.get(kind)
                if not isinstance(component, dict):
                    continue
                for source in component.get('runtime_factor_sources') or []:
                    if not isinstance(source, dict) or not _number(source.get('factor')):
                        raise ValueError('exporter 原生乘数来源结构无效。')
                    if ((source.get('source_spell_id'), source.get('effect_index')) in excluded
                            and not math.isclose(source['factor'], 1.0, rel_tol=0, abs_tol=1e-12)):
                        raise ValueError('exporter 已排除的全局效果仍参与实际伤害乘法。')
