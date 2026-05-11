import csv
import html
import io
import json
import re

import requests
from django.conf import settings
from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.models import WowSkillDiffReport
from utils.log import logger


class WagoSkillDiffMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.default_branch = 'wow'
        self.locale = str(getattr(settings, 'WAGO_SKILL_DIFF_LOCALE', 'enUS') or 'enUS')
        self.http_timeout = int(getattr(settings, 'WAGO_SKILL_DIFF_TIMEOUT', 30) or 30)
        self._chr_classes_cache = {}
        self._skilllineability_cache = {}
        self._chr_specialization_cache = {}
        self._specialization_spells_cache = {}
        self._spell_class_options_cache = {}
        self._spellclassset_to_class_cache = {}
        self.core_tables = {
            'spell',
            'spelleffect',
            'spellname',
            'spelldescription',
            'spellmisc',
            'spellauraoptions',
            'spellpower',
            'spellcooldowns',
            'spellcasttimes',
            'spellduration',
            'spellrange',
            'spelltargetrestrictions',
            'spellcategories',
            'spellscaling',
            'spellequippeditems',
            'spellinterrupts',
            'specializationspells',
            'chrspecialization',
            'traitnodeentry',
            'traitdefinition',
            'traitnode',
            'traittree',
        }

    def scan(self, url):
        branch = (url or '').strip() or (getattr(self.task, 'target', '') or '').strip() or self.default_branch
        current_build = self._fetch_current_build(branch)
        if not current_build:
            return True

        last_build = (getattr(self.task, 'flag', '') or '').strip()
        if not last_build:
            self.task.flag = current_build
            self.task.save(update_fields=['flag'])
            return True

        if last_build == current_build:
            return True

        report = None
        try:
            report = self._generate_report(branch, last_build, current_build)
        except Exception as e:
            logger.error(f"[WagoSkillDiffMonitor] generate report failed: {e}")

        self.task.flag = current_build
        self.task.save(update_fields=['flag'])

        if report:
            try:
                WowSkillDiffReport.objects.update_or_create(
                    branch=branch,
                    locale=self.locale,
                    to_build=current_build,
                    defaults={
                        'from_build': last_build,
                        'content_md': report.get('content_md') or '',
                        'changed_tables_json': report.get('changed_tables_json') or '',
                        'spell_count': int(report.get('spell_count') or 0),
                        'class_count': int(report.get('class_count') or 0),
                    }
                )
            except Exception as e:
                logger.warning(f"[WagoSkillDiffMonitor] save WowSkillDiffReport failed: {e}")

        return True

    def _fetch_current_build(self, branch):
        resp = self._http_get_text('https://wago.tools/')
        if not resp:
            return ''
        props = self._extract_inertia_props(resp)
        versions = props.get('versions') or []
        for v in versions:
            if (v.get('product') or '').strip() == branch:
                return (v.get('version') or '').strip()
        return ''

    def _generate_report(self, branch, from_build, to_build):
        changed_tables = set(self._fetch_changed_db2_tables(from_build, to_build) or [])
        relevant_tables = [t for t in sorted(changed_tables) if (t or '').lower() in self.core_tables]
        if not relevant_tables:
            for t in sorted(self.core_tables):
                diff_rows = self._fetch_db2_diff_rows(t, from_build, to_build)
                if diff_rows:
                    relevant_tables.append(t)
                    changed_tables.add(t)
        if not relevant_tables:
            return None

        class_names = self._load_chr_classes(to_build)
        spec_to_class = self._load_chr_specialization_to_class(to_build)
        spell_to_specs = self._load_specialization_spells(to_build)
        if not spec_to_class or not spell_to_specs:
            return None

        whitelist = self._field_whitelist()
        spell_changes = {}

        for t in relevant_tables:
            diff_rows = self._fetch_db2_diff_rows(t, from_build, to_build)
            if not diff_rows:
                continue
            tkey = t.lower()
            for row in diff_rows:
                spell_id = self._extract_spell_id(tkey, row)
                if not spell_id:
                    continue
                if not self._spell_has_class(spell_id, spell_to_specs, spec_to_class, to_build):
                    continue
                entry = spell_changes.setdefault(spell_id, {'tables': set(), 'diffs': {}})
                entry['tables'].add(tkey)
                record_id = self._extract_record_id(tkey, row, spell_id)
                if record_id is None:
                    continue
                action = (row.get('Action') or '').strip()
                old_data = row.get('oldData')
                if action in ('changed', 'removed') and isinstance(old_data, dict):
                    before = old_data
                    after = {k: v for k, v in row.items() if k not in ('oldData',)}
                elif action == 'added':
                    before = {}
                    after = {k: v for k, v in row.items() if k not in ('oldData',)}
                else:
                    before = self._fetch_db2_row_by_id(t, from_build, record_id)
                    after = self._fetch_db2_row_by_id(t, to_build, record_id)
                fields = whitelist.get(tkey)
                if not fields:
                    fields = sorted(set((before or {}).keys()) | set((after or {}).keys()))
                    fields = [x for x in fields if x and x not in ('ID', 'Action', 'oldData')]
                diffs = self._diff_fields(fields, before, after)
                if diffs:
                    entry['diffs'].setdefault(tkey, []).append({'id': record_id, 'action': action, 'fields': diffs})

        if not spell_changes:
            return None

        name_cache = {}

        lines = []
        server_title = self._branch_title(branch)
        lines.append(f"# {server_title} 职业技能变更报告：{from_build} → {to_build}")
        lines.append("")

        class_to_spells = {}
        for spell_id in spell_changes.keys():
            for cid in self._spell_class_ids(spell_id, spell_to_specs, spec_to_class, to_build):
                class_to_spells.setdefault(cid, set()).add(spell_id)

        class_count = len(class_to_spells)
        for cid in sorted(class_to_spells.keys()):
            cname = class_names.get(cid) or str(cid)
            lines.append(f"## {cname} ({cid})")
            lines.append("")
            for spell_id in sorted(class_to_spells[cid]):
                sname = name_cache.get(spell_id)
                if sname is None:
                    sname = self._fetch_spell_name(to_build, spell_id) or self._fetch_spell_name(from_build, spell_id) or str(spell_id)
                    name_cache[spell_id] = sname
                lines.append(f"### {sname} ({spell_id})")
                tables = sorted(spell_changes[spell_id]['tables'])
                diffs_by_table = spell_changes[spell_id]['diffs']
                for tkey in tables:
                    if tkey not in diffs_by_table:
                        continue
                    for item in diffs_by_table[tkey]:
                        action = (item.get('action') or '').strip()
                        action_part = f" ({action})" if action else ""
                        lines.append(f"- {tkey}#{item.get('id')}{action_part}")
                        for fd in item.get('fields') or []:
                            lines.append(f"  - {fd['field']}: {fd['before']} → {fd['after']}")
                lines.append("")

        content_md = "\n".join(lines).rstrip() + "\n"
        return {
            'content_md': content_md,
            'changed_tables_json': json.dumps(sorted(changed_tables), ensure_ascii=False),
            'spell_count': len(spell_changes),
            'class_count': class_count,
        }

    def _branch_title(self, branch):
        m = {
            'wow': 'Retail(正式服)',
            'wow_beta': 'Beta(测试服)',
            'wowt': 'PTR(测试服)',
            'wowxptr': 'PTR X(测试服)',
        }
        return m.get(branch, branch)

    def _fetch_changed_db2_tables(self, from_build, to_build):
        url = f"https://wago.tools/builds-diff?to={to_build}&from={from_build}"
        text = self._http_get_text(url)
        if not text:
            return set()
        props = self._extract_inertia_props(text)
        items = props.get('items') or {}
        if isinstance(items, dict):
            items = items.get('data') or []
        tables = set()
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            if (it.get('Type') or '').strip() != 'db2':
                continue
            filename = (it.get('Filename') or '').strip()
            if not filename.lower().startswith('dbfilesclient/') or not filename.lower().endswith('.db2'):
                continue
            table = filename.split('/')[-1][:-4]
            if table:
                tables.add(table)
        return tables

    def _fetch_db2_diff_rows(self, table, from_build, to_build):
        url = f"https://wago.tools/db2/{table}/diff?from={from_build}&to={to_build}"
        rows = []
        next_url = url
        visited = set()
        max_rows = int(getattr(settings, 'WAGO_SKILL_DIFF_MAX_DIFF_ROWS', 10000) or 10000)
        while next_url and next_url not in visited and len(rows) < max_rows:
            visited.add(next_url)
            text = self._http_get_text(next_url)
            if not text:
                break
            props = self._extract_inertia_props(text)
            entries = props.get('entries') or {}
            data = []
            if isinstance(entries, dict):
                data = entries.get('data') or []
                next_url = entries.get('next_page_url')
            elif isinstance(entries, list):
                data = entries
                next_url = None
            else:
                next_url = None
            if isinstance(data, list) and data:
                rows.extend(data)
            if next_url and next_url.startswith('/'):
                next_url = "https://wago.tools" + next_url
            if next_url and 'from=' not in next_url and 'to=' not in next_url:
                sep = '&' if '?' in next_url else '?'
                next_url = f"{next_url}{sep}from={from_build}&to={to_build}"
        return rows

    def _load_skilllineability_spell_classmask(self, build):
        build = (build or '').strip()
        if build in self._skilllineability_cache:
            return self._skilllineability_cache.get(build) or {}
        url = f"https://wago.tools/db2/SkillLineAbility/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._skilllineability_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._skilllineability_cache[build] = {}
            return {}
        reader = csv.DictReader(io.StringIO(text))
        m = {}
        for row in reader:
            try:
                spell_id = int(row.get('Spell') or '0')
                class_mask = int(row.get('ClassMask') or '0')
            except Exception:
                continue
            if spell_id and class_mask:
                m[spell_id] = class_mask
        self._skilllineability_cache[build] = m
        return m

    def _load_chr_classes(self, build):
        build = (build or '').strip()
        if build in self._chr_classes_cache:
            return self._chr_classes_cache.get(build) or {}
        url = f"https://wago.tools/db2/ChrClasses/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._chr_classes_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._chr_classes_cache[build] = {}
            return {}
        reader = csv.DictReader(io.StringIO(text))
        out = {}
        for row in reader:
            try:
                cid = int(row.get('ID') or '0')
            except Exception:
                continue
            if cid <= 0:
                continue
            name = (row.get('Name_lang') or row.get('Name_male_lang') or '').strip()
            if name:
                out[cid] = name
        self._chr_classes_cache[build] = out
        return out

    def _load_chr_specialization_to_class(self, build):
        build = (build or '').strip()
        if build in self._chr_specialization_cache:
            return self._chr_specialization_cache.get(build) or {}
        url = f"https://wago.tools/db2/ChrSpecialization/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._chr_specialization_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._chr_specialization_cache[build] = {}
            return {}
        reader = csv.DictReader(io.StringIO(text))
        out = {}
        for row in reader:
            try:
                spec_id = int(row.get('ID') or '0')
            except Exception:
                continue
            if spec_id <= 0:
                continue
            class_id = 0
            for k in ('ClassID', 'ChrClassesID', 'Class'):
                v = row.get(k)
                if v is None:
                    continue
                try:
                    class_id = int(v or 0)
                    break
                except Exception:
                    continue
            if class_id > 0:
                out[spec_id] = class_id
        self._chr_specialization_cache[build] = out
        return out

    def _load_specialization_spells(self, build):
        build = (build or '').strip()
        if build in self._specialization_spells_cache:
            return self._specialization_spells_cache.get(build) or {}
        url = f"https://wago.tools/db2/SpecializationSpells/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._specialization_spells_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._specialization_spells_cache[build] = {}
            return {}
        reader = csv.DictReader(io.StringIO(text))
        out = {}
        for row in reader:
            spell_id = 0
            spec_id = 0
            for k in ('SpellID', 'Spell'):
                v = row.get(k)
                if v is None:
                    continue
                try:
                    spell_id = int(v or 0)
                    break
                except Exception:
                    continue
            for k in ('SpecID', 'ChrSpecializationID', 'SpecializationID'):
                v = row.get(k)
                if v is None:
                    continue
                try:
                    spec_id = int(v or 0)
                    break
                except Exception:
                    continue
            if spell_id > 0 and spec_id > 0:
                out.setdefault(spell_id, set()).add(spec_id)
        self._specialization_spells_cache[build] = out
        return out

    def _load_spell_class_set(self, build):
        build = (build or '').strip()
        if build in self._spell_class_options_cache:
            return self._spell_class_options_cache.get(build) or {}
        url = f"https://wago.tools/db2/SpellClassOptions/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._spell_class_options_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._spell_class_options_cache[build] = {}
            return {}
        reader = csv.DictReader(io.StringIO(text))
        out = {}
        for row in reader:
            try:
                spell_id = int(row.get('SpellID') or '0')
                cls_set = int(row.get('SpellClassSet') or '0')
            except Exception:
                continue
            if spell_id > 0 and cls_set > 0:
                out[spell_id] = cls_set
        self._spell_class_options_cache[build] = out
        return out

    def _load_spellclassset_to_class(self, build, spell_to_specs, spec_to_class):
        build = (build or '').strip()
        if build in self._spellclassset_to_class_cache:
            return self._spellclassset_to_class_cache.get(build) or {}
        spell_to_set = self._load_spell_class_set(build)
        if not spell_to_set:
            self._spellclassset_to_class_cache[build] = {}
            return {}
        votes = {}
        for spell_id, specs in (spell_to_specs or {}).items():
            cls_set = spell_to_set.get(spell_id)
            if not cls_set:
                continue
            for sid in specs:
                cid = spec_to_class.get(sid)
                if not cid:
                    continue
                votes.setdefault(cls_set, {}).setdefault(cid, 0)
                votes[cls_set][cid] += 1
        out = {}
        for cls_set, m in votes.items():
            best = None
            for cid, cnt in m.items():
                if best is None or cnt > best[1]:
                    best = (cid, cnt)
            if best:
                out[cls_set] = best[0]
        self._spellclassset_to_class_cache[build] = out
        return out

    def _spell_class_ids(self, spell_id, spell_to_specs, spec_to_class, build):
        out = set()
        specs = spell_to_specs.get(spell_id)
        if not specs:
            specs = []
        for sid in specs:
            cid = spec_to_class.get(sid)
            if cid:
                out.add(cid)
        if out:
            return out
        spell_to_set = self._load_spell_class_set(build)
        cls_set = spell_to_set.get(spell_id)
        if not cls_set:
            return out
        set_to_class = self._load_spellclassset_to_class(build, spell_to_specs, spec_to_class)
        cid = set_to_class.get(cls_set)
        if cid:
            out.add(cid)
        return out

    def _spell_has_class(self, spell_id, spell_to_specs, spec_to_class, build):
        return bool(self._spell_class_ids(spell_id, spell_to_specs, spec_to_class, build))

    def _fetch_spell_name(self, build, spell_id):
        row = self._fetch_db2_row_by_id('SpellName', build, spell_id)
        if row:
            name = (row.get('Name_lang') or '').strip()
            if name:
                return name
        row = self._fetch_db2_row_by_id('spellname', build, spell_id)
        if row:
            name = (row.get('Name_lang') or '').strip()
            if name:
                return name
        return ''

    def _fetch_db2_row_by_id(self, table, build, record_id):
        record_id = str(record_id).strip()
        if not record_id:
            return {}
        url = f"https://wago.tools/db2/{table}?build={build}&locale={self.locale}&filter%5BID%5D=exact%3A{record_id}"
        text = self._http_get_text(url, timeout=max(60, self.http_timeout))
        if not text:
            return {}
        props = self._extract_inertia_props(text)
        data = []
        if 'entries' in props:
            entries = props.get('entries') or {}
            data = entries.get('data') if isinstance(entries, dict) else (entries if isinstance(entries, list) else [])
        elif 'data' in props:
            payload = props.get('data')
            data = payload.get('data') if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        if isinstance(data, list) and data:
            row = data[0]
            return row if isinstance(row, dict) else {}
        return {}

    def _extract_inertia_props(self, html_text):
        html_text = html_text or ''
        m = re.search(r'data-page="([^"]+)"', html_text)
        if not m:
            return {}
        try:
            obj = json.loads(html.unescape(m.group(1)))
        except Exception:
            return {}
        props = obj.get('props') or {}
        return props

    def _field_whitelist(self):
        return {
            'spell': [],
            'spellname': [],
            'spelldescription': [],
            'spelleffect': [],
            'spellmisc': [],
            'skilllineability': [],
        }

    def _diff_fields(self, fields, before, after):
        before = before or {}
        after = after or {}
        out = []
        for f in fields:
            bv = before.get(f, '')
            av = after.get(f, '')
            if str(bv) != str(av):
                out.append({'field': f, 'before': str(bv), 'after': str(av)})
        return out

    def _extract_spell_id(self, table_key, row):
        for k in ('SpellID', 'Spell', 'spellid', 'spell'):
            v = row.get(k)
            if v is None:
                continue
            try:
                iv = int(str(v).strip() or '0')
            except Exception:
                iv = 0
            if iv:
                return iv
        if table_key in ('spell', 'spellname', 'spelldescription', 'spellmisc'):
            v = row.get('ID')
            try:
                iv = int(str(v).strip() or '0')
            except Exception:
                iv = 0
            if iv:
                return iv
        return 0

    def _extract_record_id(self, table_key, row, spell_id):
        v = row.get('ID')
        if v is None:
            return None
        try:
            rid = int(str(v).strip() or '0')
        except Exception:
            rid = 0
        if rid:
            return rid
        if table_key in ('spell', 'spellname', 'spelldescription', 'spellmisc', 'skilllineability'):
            return spell_id
        return None

    def _class_ids_from_mask(self, mask):
        out = []
        try:
            m = int(mask or 0)
        except Exception:
            return out
        for cid in range(1, 20):
            bit = 1 << (cid - 1)
            if m & bit:
                out.append(cid)
        return out

    def _http_get_text(self, url, timeout=None):
        timeout = timeout or self.http_timeout
        try:
            resp = self.req.getResponse(url, '', headers={'Accept': 'text/html'}) if getattr(self, 'req', None) else None
            if resp is not None:
                return resp.text or ''
        except Exception:
            pass
        try:
            r = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                return ''
            return r.text or ''
        except Exception:
            return ''

    def _http_get_bytes(self, url, timeout=None):
        timeout = timeout or self.http_timeout
        try:
            resp = self.req.getResponse(url, '', headers={'Accept': '*/*'}) if getattr(self, 'req', None) else None
            if resp is not None:
                return resp.content
        except Exception:
            pass
        try:
            r = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                return b''
            return r.content
        except Exception:
            return b''

    def _parse_first_table_rows(self, html_text):
        html_text = html_text or ''
        rows = []
        table_m = re.search(r"<table[^>]*>.*?</table>", html_text, flags=re.IGNORECASE | re.DOTALL)
        if not table_m:
            return rows
        table_html = table_m.group(0)
        headers = [self._strip_tags(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", table_html, flags=re.IGNORECASE | re.DOTALL)]
        if not headers:
            return rows
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL):
            if '<td' not in tr.lower():
                continue
            cols = [self._strip_tags(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.IGNORECASE | re.DOTALL)]
            if not cols:
                continue
            if len(cols) < len(headers):
                cols = cols + [''] * (len(headers) - len(cols))
            row = {headers[i]: cols[i] for i in range(min(len(headers), len(cols)))}
            rows.append(row)
        return rows

    def _strip_tags(self, s):
        s = s or ''
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s)
        return str(s).replace('\xa0', ' ').strip()
