"""Read-only continuous Hotfix rows projected from completed report facts.

The report's frozen source_facts_json remains the only persisted source of truth.
"""
import json
import re
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from botend.controller.plugins.wow.wago_regions import wago_region_name
from botend.services.wago_db2.schema import WagoDB2Schema
from botend.services.wago_hotfix_reader_fields import FIELDS, display_hotfix_value, project_hotfix_columns

SCHEMA = WagoDB2Schema()
SOURCE_STATUSES = {2: 'Delete（仅来源状态）', 3: 'Invalidate（仅来源状态）',
                   4: '未公开（仅来源状态）'}
BASELINE_CARD = re.compile(
    r'<article\b(?=[^>]*\bdata-baseline-key\s*=)[^>]*>.*?</article\s*>',
    flags=re.IGNORECASE | re.DOTALL,
)


def _text(raw):
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False, separators=(',', ':'))
    return '' if raw is None else str(raw).strip()


def _label(table, field):
    name = dict(FIELDS.get(table.lower(), ())).get(field) or SCHEMA.field_label(field)
    return f'{name} / {field}' if name != field else field


def _field(table, key, after, before=None, *, changed=False):
    label = _label(table, key)
    new = display_hotfix_value(key, after)
    old = display_hotfix_value(key, before) if changed else None
    return {'key': key, 'label': label, 'before': _text(before) if changed else None,
            'after': _text(after), 'text': f'{old} → {new}' if changed else new}


def _fields(table, fact):
    after = fact.get('after') if isinstance(fact.get('after'), dict) else {}
    changes = fact.get('changes') or []
    if fact.get('before_verified') and fact.get('after_verified') and changes:
        return [_field(table, change['field'], change.get('after'), change.get('before'), changed=True)
                for change in changes if isinstance(change, dict) and change.get('field')][:30]
    if not fact.get('after_verified'):
        return []
    visible = []
    seen = set()
    for field in project_hotfix_columns(table, after, max_fields=12):
        key = field['field']
        if key in after and key not in seen:
            visible.append(_field(table, key, after[key]))
            seen.add(key)
    # Generic DB2 tables still expose meaningful non-zero frozen columns, not a
    # guessed interpretation. Keep the original key beside its centralized label.
    priority = ('Name_lang', 'Display_lang', 'Description_lang', 'SpellID',
                'EffectIndex', 'CreatureID', 'ItemID', 'RecoveryTime')
    remaining = sorted((key for key in after if key not in seen and key not in ('ID', 'VerifiedBuild')),
                       key=lambda key: (key not in priority, priority.index(key) if key in priority else key))
    for key in remaining:
        if len(visible) >= 12:
            break
        raw = after[key]
        if raw is None or _text(raw) in ('', '0', '0.0'):
            continue
        visible.append(_field(table, key, raw))
    return visible


def project_report_entries(report, facts):
    """Project all physical source facts; no network or client DB2 lookup."""
    if not isinstance(facts, list) or len(facts) != int(report['entry_count'] or 0):
        raise ValueError(f"Hotfix report #{report['id']} frozen source count mismatch")
    result = []
    for fact in facts:
        source = fact.get('source') if isinstance(fact, dict) else None
        if not isinstance(source, dict):
            raise ValueError(f"Hotfix report #{report['id']} has malformed frozen source")
        table = str(source.get('table_name') or '').strip()
        push, rid = int(source.get('push_id') or 0), int(source.get('record_id') or 0)
        if not table or push <= 0 or rid <= 0:
            raise ValueError(f"Hotfix report #{report['id']} has missing physical identity")
        after = fact.get('after') if isinstance(fact.get('after'), dict) else {}
        source_build = fact.get('source_build') or ''
        if not source_build:
            build_num = str(source.get('build') or '')
            source_build = (report['build_str'] if build_num == str(report['build_num'] or '')
                            and report['build_str'] else build_num)
        confirmed = bool(fact.get('after_verified') and fact.get('before_verified') and fact.get('changes'))
        kind = ('change' if confirmed else 'new_value' if fact.get('after_verified')
                else 'status' if source.get('data') is None and int(source.get('status') or 0) in SOURCE_STATUSES
                else 'unresolved')
        spell_id = None
        if fact.get('after_verified'):
            spell_id = after.get('SpellID')
            if not spell_id and table.lower() in ('spell', 'spellname', 'spelldescription'):
                spell_id = after.get('ID')
        try:
            spell_id = int(spell_id) if spell_id not in (None, '') else None
        except (ValueError, TypeError):
            spell_id = None
        title = (after.get('Name_lang') or after.get('Display_lang') or
                 after.get('DisplayName_lang') or after.get('OverrideName_lang') or '') if fact.get('after_verified') else ''
        title = _text(title) or (f'技能 #{spell_id}' if spell_id else f'{SCHEMA.table_label(table)} #{rid}')
        fields = _fields(table, fact)
        source_id = source.get('id')
        date = _text(source.get('created_at'))[:19].replace('T', ' ')
        status = int(source.get('status') or 0)
        result.append({
            'source_id': source_id, 'report_id': report['id'],
            'branch': report['branch'], 'locale': report['locale'], 'region_id': report['region_id'],
            'region_name': wago_region_name(report['region_id']) or '区域未核实',
            'table': table, 'record_id': rid, 'spell_id': spell_id,
            'build': source_build, 'push': push, 'time': date,
            'title': title, 'kind': kind, 'fields': fields,
            'status_label': ('已核实旧→新' if kind == 'change' else
                             '仅本次值（旧值未知）' if kind == 'new_value' else
                             SOURCE_STATUSES.get(status, '来源未解码') if kind == 'status' else '未解码'),
            'report_url': f"/portal/wow-hotfix-report/{report['id']}/",
            'source_url': 'https://wago.tools/hotfixes?search=' +
                          quote(f"{source.get('locale') or report['locale']} {push}"),
            '_raw_search': {key: _text(value) for key, value in after.items()
                            if key not in ('ID', 'VerifiedBuild')},
        })
    return result


def _overlay_published_client_baselines(report, rows):
    """Reuse the guarded report's read-only client comparison; never freeze it."""
    relative = str(report.get('content_html_path') or '')
    if not relative.startswith('portal/reports/'):
        return
    root = (Path(settings.BASE_DIR) / 'static').resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return
    keys = {(row['table'].lower(), row['build'], row['locale'],
             row['record_id'], row['push']): row for row in rows if row['kind'] == 'new_value'}
    if not keys:
        return
    content = path.read_text(encoding='utf-8')
    for match in BASELINE_CARD.finditer(content):
        card = BeautifulSoup(match.group(0), 'html.parser').find('article')
        if not card or 'reader-impact-card' not in (card.get('class') or []):
            continue
        parts = (card.get('data-baseline-key') or '').split(':')
        if len(parts) != 5:
            continue
        try:
            identity = (parts[0].lower(), parts[1], parts[2], int(parts[3]), int(parts[4]))
        except ValueError:
            continue
        row = keys.get(identity)
        if not row:
            continue
        raw_fields = {field['key']: field for field in row['fields']}
        raw_values = row.get('_raw_search') or {}
        compared = []
        for item in card.select('ul > li'):
            label, value = item.find('span'), item.find('strong')
            if not label or not value:
                continue
            name, text = label.get_text(' ', strip=True), value.get_text(' ', strip=True)
            field = name.rsplit('/', 1)[-1].strip()
            if (field not in raw_fields and field not in raw_values) or '→' not in text:
                continue
            original = raw_fields.get(field) or _field(row['table'], field, raw_values[field])
            compared.append({**original, 'label': name,
                             'text': text, 'comparison': 'client_baseline'})
        if compared:
            row['fields'] = compared + [field for field in row['fields']
                                        if field['key'] not in {old['key'] for old in compared}]
            row['status_label'] = '同 build 客户端基表对照（非线上前态）'


def frozen_report_entries(report):
    """Cache a derived, disposable projection, never a second persisted fact."""
    key = (f"wow-hotfix-entries-v2:{report['id']}:{report['updated_at'].timestamp()}:"
           f"{report.get('content_html_path') or ''}")
    rows = cache.get(key)
    if rows is None:
        from botend.models import WowHotfixReport
        raw = WowHotfixReport.objects.values_list('source_facts_json', flat=True).get(pk=report['id'])
        try:
            facts = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Hotfix report #{report['id']} frozen source is invalid JSON") from exc
        rows = project_report_entries(report, facts)
        _overlay_published_client_baselines(report, rows)
        cache.set(key, rows, timeout=300)
    return rows


def continuous_entries(reports):
    """Merge report intervals by exact source identity, sorted by physical push."""
    merged = {}
    names = {}
    for report in reports:
        for row in frozen_report_entries(report):
            identity = (row['branch'], row['locale'], row['region_id'],
                        row['source_id'] if row['source_id'] is not None else
                        (row['table'].lower(), row['record_id'], row['push'], row['build']))
            if identity not in merged:
                merged[identity] = row.copy()
            if row['table'].lower() == 'spellname' and row['kind'] in ('change', 'new_value') and row['spell_id']:
                names[(row['branch'], row['locale'], row['region_id'], row['push'],
                       row['build'], row['spell_id'])] = row['title']
    entries = list(merged.values())
    for row in entries:
        if row['spell_id'] and row['title'] == f"技能 #{row['spell_id']}":
            name = names.get((row['branch'], row['locale'], row['region_id'],
                              row['push'], row['build'], row['spell_id']))
            if name:
                row['title'] = name
    entries.sort(key=lambda row: (row['push'], row['time'], row['source_id'] or 0,
                                  row['record_id']), reverse=True)
    return entries


def public_hotfix_entry(row, query=''):
    """Return one bounded row; reveal only a matching hidden DB2 field."""
    public = {key: value for key, value in row.items() if not key.startswith('_')}
    public['fields'] = list(row['fields'])
    if query:
        visible = {field['key'] for field in public['fields']}
        for key, value in row.get('_raw_search', {}).items():
            if query in key.casefold() and key not in visible:
                public['fields'].insert(0, _field(row['table'], key, value))
                break
    return public
