"""生成、校验并幂等导入 SimC APL 中英文本地化数据包。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from django.db import connection, transaction
from django.utils import timezone

from botend.models import SimcAplSymbol


PACKAGE_SCHEMA_VERSION = 1
SUPPORTED_KINDS = frozenset({
    SimcAplSymbol.KIND_ACTION,
    SimcAplSymbol.KIND_BUFF,
    SimcAplSymbol.KIND_DEBUFF,
    SimcAplSymbol.KIND_DOT,
    SimcAplSymbol.KIND_COOLDOWN,
})
CLASS_ALIASES = {
    'death_knight': 'deathknight',
    'demon_hunter': 'demonhunter',
}
IDENTITY_SOURCE_PRIORITY = (
    'runtime_apl_action',
    'runtime_action_probe',
    'runtime_expression_binding',
    'runtime_expression_resolver',
    'runtime_buff_registry',
    'runtime_fallback_buff',
    'static_source_registration',
)
DEFAULT_PACKAGE_DIR = Path(__file__).resolve().parents[2] / 'data' / 'simc_apl_metadata'


@dataclass(frozen=True)
class ImportSummary:
    package_facts: int
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    manual_preserved: int = 0
    deactivated: int = 0


def _canonical_class(value):
    key = str(value or '').strip().lower()
    return CLASS_ALIASES.get(key, key)


def _positive_int_or_none(value, field):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f'{field} 必须为正整数或 null')
    return value


def _source_record_signature(record):
    """只有会改变字段身份或可用性的内容完全相同时才允许折叠作用域。"""
    return (
        record.get('spell_id'),
        tuple(record.get('spell_id_candidates') or ()),
        str(record.get('name_zh') or ''),
        str(record.get('wowhead_status') or ''),
        tuple(record.get('aliases') or ()),
        tuple(record.get('action_options') or ()),
        tuple(record.get('expression_suffixes') or ()),
        json.dumps(record.get('source_metadata') or {}, ensure_ascii=False, sort_keys=True),
    )


def _preferred_identity_source(values: Iterable[str]) -> str:
    available = {str(value or '').strip() for value in values if str(value or '').strip()}
    for source in IDENTITY_SOURCE_PRIORITY:
        if source in available:
            return source
    return sorted(available)[0] if available else ''


def _normalized_source_record(record, official_specs, index):
    if not isinstance(record, dict):
        raise ValueError(f'records[{index}] 必须为对象')
    class_name = str(record.get('class') or '').strip().lower()
    spec = str(record.get('spec') or '').strip().lower()
    kind = str(record.get('kind') or '').strip().lower()
    token = str(record.get('token') or '').strip().lower()
    if class_name not in official_specs or spec not in official_specs[class_name]:
        raise ValueError(f'records[{index}] 的职业/专精不在 official_specs 中')
    if kind not in SUPPORTED_KINDS or not token:
        raise ValueError(f'records[{index}] 的 kind/token 无效')
    values = dict(record)
    values.update(class_name=class_name, spec=spec, kind=kind, token=token)
    values['spell_id'] = _positive_int_or_none(record.get('spell_id'), f'records[{index}].spell_id')
    candidates = record.get('spell_id_candidates') or []
    if not isinstance(candidates, list):
        raise ValueError(f'records[{index}].spell_id_candidates 必须为数组')
    normalized_candidates = []
    for value in candidates:
        candidate = _positive_int_or_none(
            value, f'records[{index}].spell_id_candidates',
        )
        if candidate is None:
            raise ValueError(f'records[{index}].spell_id_candidates 不能包含 null')
        normalized_candidates.append(candidate)
    values['spell_id_candidates'] = sorted(set(normalized_candidates))
    for field in ('sources', 'identity_reasons', 'aliases', 'action_options', 'expression_suffixes'):
        field_values = record.get(field) or []
        if not isinstance(field_values, list) or not all(isinstance(value, str) for value in field_values):
            raise ValueError(f'records[{index}].{field} 必须为字符串数组')
        values[field] = sorted({value.strip() for value in field_values if value.strip()})
    for field in ('name_zh', 'wowhead_status', 'wowhead_raw_name', 'wowhead_url',
                  'apl_field', 'apl_expression_template', 'class_zh', 'spec_zh',
                  'identity_status', 'localization_source'):
        if record.get(field) is not None and not isinstance(record.get(field), str):
            raise ValueError(f'records[{index}].{field} 必须为字符串')
        values[field] = str(record.get(field) or '').strip()
    source_metadata = record.get('source_metadata') or {}
    if not isinstance(source_metadata, dict):
        raise ValueError(f'records[{index}].source_metadata 必须为对象')
    values['source_metadata'] = source_metadata
    return values


def _package_fact(rows, scope, official_specs):
    rows = sorted(rows, key=lambda item: item['spec'])
    first = rows[0]
    class_name = first['class_name']
    covered_specs = sorted({row['spec'] for row in rows})
    sources = sorted({source for row in rows for source in row['sources']})
    reasons = sorted({reason for row in rows for reason in row['identity_reasons']})
    spec = first['spec'] if scope == 'spec' else None
    name_zh = first['name_zh']
    wowhead_status = first['wowhead_status'] or ('not_requested' if first['spell_id'] else 'unbound')
    localization_source = first['localization_source'] or (
        'wowhead' if wowhead_status in {'ok', 'unlocalized', 'empty', 'missing'} else ''
    )
    metadata = {
        'apl_field': first['apl_field'],
        'apl_expression_template': first['apl_expression_template'],
        'expression_suffixes': first['expression_suffixes'],
        'class_zh': first['class_zh'],
        'spec_zh': first['spec_zh'] if scope == 'spec' else '',
        'covered_specs': covered_specs,
        'covered_specs_zh': {
            row['spec']: row['spec_zh'] for row in sorted(rows, key=lambda item: item['spec'])
        },
        'wowhead_raw_name': first['wowhead_raw_name'],
        'wowhead_url': first['wowhead_url'],
        'source_facts': sources,
        'identity_reasons': reasons,
        'input_identity_status': first['identity_status'],
    }
    if first['source_metadata']:
        metadata['source_coverage'] = first['source_metadata']
    return {
        'scope': scope,
        'class_name': _canonical_class(class_name),
        'spec': spec,
        'hero_tree': None,
        'token': first['token'],
        'symbol_kind': first['kind'],
        'spell_id': first['spell_id'],
        'trait_id': None,
        'source': SimcAplSymbol.SOURCE_SIMC_MANIFEST,
        'identity_source': _preferred_identity_source(sources),
        'identity_reason': reasons[0] if reasons else '',
        'identity_candidates': first['spell_id_candidates'],
        'aliases': first['aliases'],
        'options': first['action_options'],
        # APL token 是始终存在、可逆且不会被本地化覆盖的英文身份。
        'name_en': first['token'],
        'name_zh': name_zh,
        'localization_source': localization_source,
        'localization_status': wowhead_status,
        'metadata': metadata,
    }


def _package_counts(facts, source_record_count, official_spec_count):
    return {
        'source_record_count': source_record_count,
        'fact_count': len(facts),
        'official_spec_count': official_spec_count,
        'scope_counts': dict(sorted(Counter(fact['scope'] for fact in facts).items())),
        'kind_counts': dict(sorted(Counter(fact['symbol_kind'] for fact in facts).items())),
        'bound_count': sum(fact['spell_id'] is not None for fact in facts),
        'unbound_count': sum(fact['spell_id'] is None for fact in facts),
        'localized_count': sum(bool(fact['name_zh']) for fact in facts),
        'missing_zh_count': sum(not fact['name_zh'] for fact in facts),
    }


def build_metadata_package(source_payload):
    """把逐专精字段导出折叠成可直接导入数据库的稳定数据包。"""
    if not isinstance(source_payload, dict) or source_payload.get('schema_version') != 1:
        raise ValueError('源 APL 字段 JSON schema_version 必须为 1')
    revision = str(source_payload.get('simc_revision') or '').strip().lower()
    game_build = str(source_payload.get('game_build') or '').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not game_build:
        raise ValueError('源数据缺少有效 simc_revision/game_build')
    raw_specs = source_payload.get('official_specs')
    if not isinstance(raw_specs, dict) or not raw_specs:
        raise ValueError('源数据缺少 official_specs')
    official_specs = {}
    for class_name, specs in raw_specs.items():
        source_class = str(class_name or '').strip().lower()
        if not source_class or not isinstance(specs, list) or not specs:
            raise ValueError('official_specs 必须是非空职业到专精数组映射')
        official_specs[source_class] = {
            str(spec or '').strip().lower() for spec in specs if str(spec or '').strip()
        }
        if len(official_specs[source_class]) != len(specs):
            raise ValueError(f'official_specs.{source_class} 含空值或重复项')
    raw_records = source_payload.get('records')
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError('源数据缺少 records')
    records = [
        _normalized_source_record(record, official_specs, index)
        for index, record in enumerate(raw_records)
    ]
    groups = defaultdict(list)
    for record in records:
        groups[(record['class_name'], record['kind'], record['token'])].append(record)

    facts = []
    for (class_name, _kind, _token), rows in sorted(groups.items()):
        seen_specs = {row['spec'] for row in rows}
        signatures = {_source_record_signature(row) for row in rows}
        forced_class_scope = (
            len(signatures) == 1 and
            {row.get('source_metadata', {}).get('scope_hint') for row in rows} == {'class'}
        )
        # 单专精职业无法从当前语料证明这是职业通用字段，保留专精作用域。
        class_scoped = (
            forced_class_scope or (
                len(official_specs[class_name]) >= 2 and
                seen_specs == official_specs[class_name] and
                len(signatures) == 1
            )
        )
        if class_scoped:
            facts.append(_package_fact(rows, 'class', official_specs))
        else:
            facts.extend(
                _package_fact([row], 'spec', official_specs)
                for row in sorted(rows, key=lambda item: item['spec'])
            )
    facts.sort(key=lambda fact: (
        fact['class_name'], 0 if fact['scope'] == 'class' else 1,
        fact['spec'] or '', fact['symbol_kind'], fact['token'],
    ))
    canonical_specs = {
        _canonical_class(class_name): sorted(specs)
        for class_name, specs in sorted(official_specs.items())
    }
    wowhead = source_payload.get('wowhead') if isinstance(source_payload.get('wowhead'), dict) else {}
    payload = {
        'schema_version': PACKAGE_SCHEMA_VERSION,
        'package_type': 'simc_apl_localization_metadata',
        'source_payload_sha256': hashlib.sha256(
            json.dumps(
                source_payload, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'),
            ).encode('utf-8')
        ).hexdigest(),
        'simc_revision': revision,
        'game_build': game_build,
        'source_generated_at': str(source_payload.get('generated_at') or ''),
        'wowhead': {
            'data_env': wowhead.get('data_env'),
            'locale': wowhead.get('locale'),
            'environment_scope': str(wowhead.get('environment_scope') or ''),
        },
        'official_specs': canonical_specs,
        'expression_suffixes': source_payload.get('expression_suffixes') or {},
        'scope_policy': {
            'default': 'spec',
            'class_when': '至少两个官方专精全部覆盖且身份、本地化、别名和参数完全一致',
            'global_enabled': False,
        },
        'facts': facts,
    }
    payload['counts'] = _package_counts(
        facts, len(records), sum(len(specs) for specs in canonical_specs.values()),
    )
    validate_metadata_package(payload)
    return payload


def _fact_identity(fact):
    return (
        fact['class_name'], fact.get('spec') or '', fact.get('hero_tree') or '',
        fact['token'], fact['symbol_kind'],
    )


def validate_metadata_package(payload):
    """在触碰数据库前严格验证完整数据包与声明计数。"""
    if not isinstance(payload, dict) or payload.get('schema_version') != PACKAGE_SCHEMA_VERSION:
        raise ValueError(f'不支持的数据包 schema_version: {payload.get("schema_version")!r}')
    if payload.get('package_type') != 'simc_apl_localization_metadata':
        raise ValueError('数据包 package_type 无效')
    if not re.fullmatch(r'[0-9a-f]{64}', str(payload.get('source_payload_sha256') or '')):
        raise ValueError('数据包 source_payload_sha256 无效')
    revision = str(payload.get('simc_revision') or '').strip().lower()
    game_build = str(payload.get('game_build') or '').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not game_build:
        raise ValueError('数据包缺少有效 simc_revision/game_build')
    official_specs = payload.get('official_specs')
    if not isinstance(official_specs, dict) or not official_specs:
        raise ValueError('数据包 official_specs 无效')
    normalized_specs = {}
    for class_name, specs in official_specs.items():
        class_name = _canonical_class(class_name)
        if not class_name or not isinstance(specs, list) or not specs:
            raise ValueError('数据包 official_specs 含无效职业或专精')
        values = [str(spec or '').strip().lower() for spec in specs]
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError(f'数据包 official_specs.{class_name} 含空值或重复项')
        normalized_specs[class_name] = set(values)
    facts = payload.get('facts')
    if not isinstance(facts, list):
        raise ValueError('数据包 facts 必须为数组')
    identities = set()
    for index, fact in enumerate(facts):
        prefix = f'facts[{index}]'
        if not isinstance(fact, dict):
            raise ValueError(f'{prefix} 必须为对象')
        scope = fact.get('scope')
        class_name = _canonical_class(fact.get('class_name'))
        spec = str(fact.get('spec') or '').strip().lower() or None
        hero_tree = str(fact.get('hero_tree') or '').strip().lower() or None
        token = str(fact.get('token') or '').strip().lower()
        kind = str(fact.get('symbol_kind') or '').strip().lower()
        if class_name not in normalized_specs or scope not in {'class', 'spec'}:
            raise ValueError(f'{prefix} 的 class_name/scope 无效')
        if scope == 'class' and (spec or hero_tree):
            raise ValueError(f'{prefix} 的 class scope 不能带 spec/hero_tree')
        if scope == 'spec' and (spec not in normalized_specs[class_name] or hero_tree):
            raise ValueError(f'{prefix} 的 spec scope 无效')
        if not token or kind not in SUPPORTED_KINDS:
            raise ValueError(f'{prefix} 的 token/symbol_kind 无效')
        if not isinstance(fact.get('name_en'), str) or not fact['name_en'].strip():
            raise ValueError(f'{prefix}.name_en 不能为空')
        for field in ('name_zh', 'localization_source', 'localization_status',
                      'identity_source', 'identity_reason'):
            if not isinstance(fact.get(field, ''), str):
                raise ValueError(f'{prefix}.{field} 必须为字符串')
        if not isinstance(fact.get('identity_candidates'), list):
            raise ValueError(f'{prefix}.identity_candidates 必须为数组')
        for value in fact['identity_candidates']:
            if _positive_int_or_none(value, f'{prefix}.identity_candidates') is None:
                raise ValueError(f'{prefix}.identity_candidates 不能包含 null')
        for field in ('aliases', 'options'):
            values = fact.get(field)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f'{prefix}.{field} 必须为字符串数组')
        if not isinstance(fact.get('metadata'), dict):
            raise ValueError(f'{prefix}.metadata 必须为对象')
        _positive_int_or_none(fact.get('spell_id'), f'{prefix}.spell_id')
        _positive_int_or_none(fact.get('trait_id'), f'{prefix}.trait_id')
        identity = (class_name, spec or '', hero_tree or '', token, kind)
        if identity in identities:
            raise ValueError(f'{prefix} 与前面的事实身份重复: {identity!r}')
        identities.add(identity)
    counts = payload.get('counts')
    source_record_count = counts.get('source_record_count') if isinstance(counts, dict) else None
    if (not isinstance(source_record_count, int) or isinstance(source_record_count, bool) or
            source_record_count < len(facts)):
        raise ValueError('数据包 counts.source_record_count 必须是不小于事实数的非负整数')
    expected_counts = _package_counts(
        facts, source_record_count, sum(len(specs) for specs in normalized_specs.values()),
    )
    if payload.get('counts') != expected_counts:
        raise ValueError('数据包 counts 与 facts/official_specs 不一致')
    return payload


def load_metadata_package(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'无法读取 APL 元数据包 {path}: {exc}') from exc
    return validate_metadata_package(payload)


def find_default_metadata_package():
    files = sorted(DEFAULT_PACKAGE_DIR.glob('*.json'))
    if len(files) != 1:
        raise ValueError(f'默认数据包目录必须且只能包含一个 JSON，当前为 {len(files)} 个')
    return files[0]


def _model_identity(symbol):
    prepared = SimcAplSymbol.prepare(symbol)
    return (
        prepared.class_key, prepared.spec_key, prepared.hero_tree_key,
        prepared.token, prepared.symbol_kind,
    )


def _import_payload(symbol):
    return (
        symbol.class_name, symbol.spec, symbol.hero_tree,
        symbol.spell_id, symbol.trait_id, symbol.source,
        symbol.identity_source, symbol.identity_reason,
        symbol.identity_candidates, symbol.aliases, symbol.options,
        symbol.name_en, symbol.name_zh, symbol.localization_source,
        symbol.localization_status, symbol.metadata, symbol.is_active,
    )


def import_metadata_package(payload, *, dry_run=False, deactivate_missing=False,
                            refresh_all=False):
    """幂等导入数据包；全量刷新只停用旧的自动生成同类事实。"""
    validate_metadata_package(payload)
    revision = payload['simc_revision']
    game_build = payload['game_build']
    identity_fields = (
        'simc_revision', 'wow_build', 'class_key', 'spec_key',
        'hero_tree_key', 'token', 'symbol_kind',
    )
    update_fields = (
        'class_name', 'spec', 'hero_tree', 'spell_id', 'trait_id', 'source',
        'identity_source', 'identity_reason', 'identity_candidates',
        'aliases', 'options', 'name_en', 'name_zh', 'localization_source',
        'localization_status', 'metadata', 'is_active', 'updated_at',
    )
    candidates = []
    for fact in payload['facts']:
        values = dict(fact)
        values.pop('scope', None)
        values['identity_reason'] = str(values.get('identity_reason') or '')[:128]
        candidate = SimcAplSymbol.prepare(SimcAplSymbol(
            simc_revision=revision,
            wow_build=game_build,
            **values,
        ))
        candidate.is_active = True
        candidates.append(candidate)
    incoming = {_model_identity(candidate): candidate for candidate in candidates}
    if len(incoming) != len(candidates):
        raise ValueError('数据包规范化后出现重复数据库身份')

    summary = None
    with transaction.atomic():
        existing_rows = list(SimcAplSymbol.objects.filter(
            simc_revision=revision, wow_build=game_build,
        ))
        existing = {_model_identity(row): row for row in existing_rows}
        manual_identities = {
            identity for identity, row in existing.items()
            if row.source == SimcAplSymbol.SOURCE_MANUAL
        }
        importable = [
            candidate for identity, candidate in incoming.items()
            if identity not in manual_identities
        ]
        created = updated = unchanged = 0
        for candidate in importable:
            identity = _model_identity(candidate)
            previous = existing.get(identity)
            if previous is None:
                created += 1
            elif _import_payload(previous) == _import_payload(candidate):
                unchanged += 1
            else:
                updated += 1

        now = timezone.now()
        for candidate in importable:
            candidate.updated_at = now
        if importable:
            bulk_kwargs = {
                'update_conflicts': True,
                'update_fields': update_fields,
                'batch_size': 1000,
            }
            if connection.features.supports_update_conflicts_with_target:
                bulk_kwargs['unique_fields'] = identity_fields
            SimcAplSymbol.objects.bulk_create(importable, **bulk_kwargs)

        stale_ids = set()
        if deactivate_missing or refresh_all:
            stale_ids.update(
                row.pk for identity, row in existing.items()
                if identity not in incoming and row.is_active and
                row.source != SimcAplSymbol.SOURCE_MANUAL and
                row.symbol_kind in SUPPORTED_KINDS
            )
        if refresh_all:
            stale_ids.update(
                SimcAplSymbol.objects.filter(
                    is_active=True, symbol_kind__in=SUPPORTED_KINDS,
                ).exclude(
                    source=SimcAplSymbol.SOURCE_MANUAL,
                ).exclude(
                    simc_revision=revision, wow_build=game_build,
                ).values_list('pk', flat=True)
            )
        deactivated = 0
        if stale_ids:
            deactivated = SimcAplSymbol.objects.filter(pk__in=stale_ids).update(
                is_active=False,
            )
        summary = ImportSummary(
            package_facts=len(payload['facts']),
            created=created,
            updated=updated,
            unchanged=unchanged,
            manual_preserved=len(manual_identities & set(incoming)),
            deactivated=deactivated,
        )
        if dry_run:
            transaction.set_rollback(True)
    return summary
