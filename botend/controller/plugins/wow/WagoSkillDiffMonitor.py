import csv
import html
import io
import json
import os
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
        self._chr_specialization_meta_cache = {}
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
        raw_branch = (url or '').strip() or (getattr(self.task, 'target', '') or '').strip()
        if raw_branch in {'-', 'auto', 'default'}:
            raw_branch = ''
        branch = raw_branch or self.default_branch
        current_build = self._fetch_current_build(branch)
        if not current_build:
            return True

        last_build = (getattr(self.task, 'flag', '') or '').strip()
        if not last_build:
            self.task.flag = current_build
            self.task.save(update_fields=['flag'])
            try:
                WowSkillDiffReport.objects.update_or_create(
                    branch=branch,
                    locale=self.locale,
                    to_build=current_build,
                    defaults={
                        'from_build': current_build,
                        'content_md': f"# {self._branch_title(branch)} 职业技能变更报告：{current_build}\n\n- 初始化：已记录当前版本号，后续版本变更时会生成差异报告。\n",
                        'content_html_path': '',
                        'changed_tables_json': '[]',
                        'spell_count': 0,
                        'class_count': 0,
                    }
                )
            except Exception as e:
                logger.warning(f"[WagoSkillDiffMonitor] save init WowSkillDiffReport failed: {e}")
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
                        'content_html_path': report.get('content_html_path') or '',
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

    def _generate_report(self, branch, from_build, to_build, display_from_build='', display_to_build='', wowhead_url=''):
        wowhead_spell_ids = set()
        if wowhead_url:
            wowhead_spell_ids = self._fetch_wowhead_spell_ids(wowhead_url)
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
        spec_meta = self._load_chr_specialization_meta(to_build)
        spec_to_class = {sid: meta.get('class_id') for sid, meta in (spec_meta or {}).items() if meta.get('class_id')}
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
                if wowhead_spell_ids and spell_id not in wowhead_spell_ids:
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
                    payload = {'id': record_id, 'action': action, 'fields': diffs}
                    if tkey == 'spelleffect':
                        meta = {}
                        for k in ('EffectIndex', 'Effect', 'EffectAura'):
                            v = (after or {}).get(k)
                            if v is None or v == '':
                                v = (before or {}).get(k)
                            if v is not None and v != '':
                                meta[k] = v
                        payload['meta'] = meta
                    entry['diffs'].setdefault(tkey, []).append(payload)

        filtered_spell_changes = {}
        for spell_id, entry in spell_changes.items():
            diffs_by_table = entry.get('diffs') or {}
            keep = False
            for tkey, items in diffs_by_table.items():
                if tkey == 'spelleffect':
                    for it in items or []:
                        if (it.get('action') or '').strip() in ('changed', 'removed'):
                            keep = True
                            break
                if keep:
                    break
            if keep:
                filtered_spell_changes[spell_id] = entry
        spell_changes = filtered_spell_changes

        if not spell_changes:
            return None

        server_title = self._branch_title(branch)
        content_md = f"# {server_title} 职业技能变更报告：{from_build} → {to_build}\n\n- 技能数：{len(spell_changes)}\n"
        html_meta = self._write_html_report(
            branch=branch,
            server_title=server_title,
            from_build=from_build,
            to_build=to_build,
            display_from_build=display_from_build,
            display_to_build=display_to_build,
            class_names=class_names,
            spec_meta=spec_meta,
            spell_to_specs=spell_to_specs,
            spec_to_class=spec_to_class,
            spell_changes=spell_changes,
            wowhead_url=wowhead_url,
        )
        content_html_path = (html_meta or {}).get('path') or ''
        class_count = int((html_meta or {}).get('class_count') or 0)
        return {
            'content_md': content_md,
            'content_html_path': content_html_path or '',
            'display_from_build': display_from_build or '',
            'display_to_build': display_to_build or '',
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

    def _load_chr_specialization_meta(self, build):
        build = (build or '').strip()
        if build in self._chr_specialization_meta_cache:
            return self._chr_specialization_meta_cache.get(build) or {}
        url = f"https://wago.tools/db2/ChrSpecialization/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._chr_specialization_meta_cache[build] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._chr_specialization_meta_cache[build] = {}
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
            name = (row.get('Name_lang') or row.get('FemaleName_lang') or '').strip()
            if class_id > 0:
                out[spec_id] = {'class_id': class_id, 'name': name or str(spec_id)}
        self._chr_specialization_meta_cache[build] = out
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

    def _fetch_wowhead_spell_ids(self, url):
        url = (url or '').strip()
        if not url:
            return set()
        def fetch(u):
            try:
                r = requests.get(u, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
            except Exception:
                return ''
            if r.status_code != 200:
                return ''
            return r.text or ''

        def extract(t):
            ids = set()
            for x in re.findall(r'(?:/spell=|spell=)(\d+)', t or ''):
                try:
                    ids.add(int(x))
                except Exception:
                    continue
            return ids

        candidates = [url]
        if 'www.wowhead.com/cn/' in url:
            candidates.append(url.replace('www.wowhead.com/cn/', 'www.wowhead.com/'))

        best = set()
        for u in candidates:
            t = fetch(u)
            ids = extract(t)
            if len(ids) > len(best):
                best = ids
            if len(best) >= 5:
                return best

            proxy_url = f"https://r.jina.ai/{u}"
            t2 = fetch(proxy_url)
            ids2 = extract(t2)
            if len(ids2) > len(best):
                best = ids2
            if len(best) >= 5:
                return best
        return best

    def _filter_diff_fields(self, table_key, fields):
        table_key = (table_key or '').lower()
        out = []
        for f in fields or []:
            name = (f.get('field') or '').strip()
            if not name:
                continue
            if name in ('Action', 'oldData'):
                continue
            if table_key == 'spellmisc':
                if name.startswith('Attributes_'):
                    continue
                if name in (
                    'SpellIconFileDataID',
                    'ActiveIconFileDataID',
                    'SpellVisualScript',
                    'ActiveSpellVisualScript',
                    'SpellVisualID',
                ):
                    continue
            if table_key == 'spelleffect':
                keep = {
                    'EffectIndex',
                    'Effect',
                    'EffectAura',
                    'EffectBasePointsF',
                    'EffectBasePoints',
                    'EffectBonusCoefficient',
                    'BonusCoefficientFromAP',
                    'Coefficient',
                    'PvpMultiplier',
                    'EffectMiscValue_0',
                    'EffectMiscValue_1',
                    'EffectAmplitude',
                    'EffectAuraPeriod',
                    'EffectTriggerSpell',
                    'EffectChainTargets',
                    'ImplicitTarget_0',
                    'ImplicitTarget_1',
                }
                if name not in keep:
                    continue
            if table_key in ('spellname',):
                if name != 'Name_lang':
                    continue
            if table_key in ('spelldescription', 'spell'):
                if name not in ('Description_lang', 'AuraDescription_lang'):
                    continue
            if table_key in ('spellcooldowns',):
                if name not in ('Cooldown', 'CooldownRecoveryTime', 'CategoryRecoveryTime', 'StartRecoveryTime', 'GCD'):
                    continue
            if table_key in ('spellpower',):
                if name not in ('PowerType', 'ManaCost', 'PowerCost', 'PowerCostPerSecond', 'PowerCostPerSecondPerLevel', 'PowerCostPct', 'PowerCostMaxPct'):
                    continue
            if table_key in ('spellduration',):
                if name not in ('Duration', 'MaxDuration'):
                    continue
            if table_key in ('spellcasttimes',):
                if name not in ('Base', 'Minimum', 'PerLevel'):
                    continue
            if table_key in ('spellrange',):
                if name not in ('RangeMin_0', 'RangeMin_1', 'RangeMax_0', 'RangeMax_1'):
                    continue
            out.append(f)
        return out

    def _expand_spell_refs(self, build, text, depth=0, visited=None):
        if depth >= 4:
            return text or ''
        visited = visited or set()
        s = str(text or '')
        for m in re.findall(r'\$@(spelldesc|spellaura)(\d+)', s):
            kind, sid = m[0], m[1]
            key = f"{kind}{sid}"
            if key in visited:
                continue
            visited.add(key)
            spell_id = int(sid or 0)
            desc_row = self._fetch_db2_row_by_id('spelldescription', build, spell_id)
            if kind == 'spelldesc':
                rep = (desc_row.get('Description_lang') or '').strip()
            else:
                rep = (desc_row.get('AuraDescription_lang') or '').strip()
            if rep:
                rep = self._expand_spell_refs(build, rep, depth=depth + 1, visited=visited)
                s = s.replace(f"$@{kind}{sid}", rep)
        return s

    def _write_html_report(self, branch, server_title, from_build, to_build, display_from_build, display_to_build, class_names, spec_meta, spell_to_specs, spec_to_class, spell_changes, wowhead_url=''):
        rel_path = f"portal/reports/wow_skill_diff_{branch}_{self.locale}_{to_build.replace('.', '_')}.html"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        name_cache = {}

        class_to_spec_to_spells = {}
        for spell_id in spell_changes.keys():
            specs = spell_to_specs.get(spell_id) or set()
            if specs:
                for spec_id in specs:
                    meta = (spec_meta or {}).get(spec_id) or {}
                    cid = int(meta.get('class_id') or 0)
                    if cid <= 0:
                        continue
                    class_to_spec_to_spells.setdefault(cid, {}).setdefault(spec_id, set()).add(spell_id)
            else:
                for cid in self._spell_class_ids(spell_id, spell_to_specs, spec_to_class, to_build):
                    class_to_spec_to_spells.setdefault(cid, {}).setdefault(0, set()).add(spell_id)

        class_count = len(class_to_spec_to_spells)

        parts = []
        parts.append('<!DOCTYPE html>')
        parts.append('<html lang="zh-CN">')
        parts.append('<head>')
        parts.append('<meta charset="UTF-8">')
        parts.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
        title_from = display_from_build or from_build
        title_to = display_to_build or to_build
        parts.append(f"<title>{html.escape(server_title)} 职业技能变更报告：{html.escape(title_from)} → {html.escape(title_to)}</title>")
        parts.append('<style>')
        parts.append('body{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:0;padding:16px;line-height:1.55;background:#0b1220;color:#0f172a}')
        parts.append('.card{max-width:1100px;margin:0 auto;background:rgba(255,255,255,.92);border:1px solid rgba(148,163,184,.4);border-radius:14px;padding:18px}')
        parts.append('.meta{color:#475569;font-size:12px;margin-top:6px;display:flex;flex-wrap:wrap;gap:10px}')
        parts.append('.toc{margin-top:12px;padding:10px 12px;background:#ffffff;border:1px solid rgba(226,232,240,1);border-radius:12px}')
        parts.append('.toc a{color:#3730a3;text-decoration:none}')
        parts.append('.toc a:hover{text-decoration:underline}')
        parts.append('h2{margin:18px 0 8px 0;font-size:18px}')
        parts.append('h3{margin:14px 0 6px 0;font-size:15px;color:#0f172a}')
        parts.append('h4{margin:12px 0 6px 0;font-size:14px}')
        parts.append('.spell{margin-top:10px;border-left:3px solid rgba(99,102,241,.5);padding-left:10px}')
        parts.append('.tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;background:#eef2ff;color:#3730a3;margin-left:8px}')
        parts.append('.subtle{color:#64748b;font-size:12px}')
        parts.append('.diff{margin-top:6px;background:#0b1220;color:#e2e8f0;border-radius:10px;padding:8px 10px;overflow:auto}')
        parts.append('.diff table{width:100%;border-collapse:collapse;font-size:12px}')
        parts.append('.diff th,.diff td{border-bottom:1px solid rgba(148,163,184,.2);padding:6px 8px;vertical-align:top}')
        parts.append('.diff th{text-align:left;color:#c7d2fe;font-weight:700}')
        parts.append('.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}')
        parts.append('</style>')
        parts.append('</head>')
        parts.append('<body>')
        parts.append('<div class="card">')
        parts.append(f"<h1 style='margin:0;font-size:18px'>{html.escape(server_title)} 职业技能变更报告：{html.escape(title_from)} → {html.escape(title_to)}</h1>")
        parts.append(f"<div class='meta'><span>技能数：{len(spell_changes)}</span><span>职业数：{class_count}</span><span>Locale：{html.escape(self.locale)}</span></div>")
        if display_from_build or display_to_build:
            parts.append(f"<div class='meta'><span class='subtle'>数据版本：{html.escape(from_build)} → {html.escape(to_build)}</span></div>")
        if wowhead_url:
            parts.append(f"<div class='meta'><a href='{html.escape(wowhead_url)}' target='_blank' rel='noopener noreferrer'>Wowhead 参考链接</a></div>")

        parts.append("<div class='toc'><div style='font-weight:800'>目录</div>")
        for cid in sorted(class_to_spec_to_spells.keys()):
            cname = (class_names or {}).get(cid) or str(cid)
            parts.append(f"<div style='margin-top:6px'><a href='#class-{cid}'>{html.escape(cname)}</a></div>")
            spec_map = class_to_spec_to_spells.get(cid) or {}
            for spec_id in sorted(spec_map.keys()):
                if spec_id == 0:
                    spec_name = '通用'
                else:
                    spec_name = ((spec_meta or {}).get(spec_id) or {}).get('name') or str(spec_id)
                parts.append(f"<div style='margin-left:14px;margin-top:4px'><a href='#class-{cid}-spec-{spec_id}'>{html.escape(spec_name)}</a></div>")
        parts.append("</div>")

        for cid in sorted(class_to_spec_to_spells.keys()):
            cname = (class_names or {}).get(cid) or str(cid)
            parts.append(f"<h2 id='class-{cid}'>{html.escape(cname)} <span class='tag'>Class {cid}</span></h2>")
            spec_map = class_to_spec_to_spells.get(cid) or {}
            for spec_id in sorted(spec_map.keys()):
                if spec_id == 0:
                    spec_name = '通用'
                else:
                    spec_name = ((spec_meta or {}).get(spec_id) or {}).get('name') or str(spec_id)
                parts.append(f"<h3 id='class-{cid}-spec-{spec_id}'>{html.escape(spec_name)} <span class='tag'>Spec {spec_id}</span></h3>")
                for spell_id in sorted(spec_map.get(spec_id) or []):
                    sname = name_cache.get(spell_id)
                    if sname is None:
                        sname = self._fetch_spell_name(to_build, spell_id) or self._fetch_spell_name(from_build, spell_id) or str(spell_id)
                        name_cache[spell_id] = sname
                    wowhead_spell_url = f"https://www.wowhead.com/spell={spell_id}"
                    parts.append(f"<div class='spell'><h4 id='spell-{spell_id}'>{html.escape(sname)} <span class='tag'>Spell {spell_id}</span> <a class='subtle' href='{html.escape(wowhead_spell_url)}' target='_blank' rel='noopener noreferrer'>Wowhead</a></h4>")

                    diffs_by_table = (spell_changes.get(spell_id) or {}).get('diffs') or {}
                    for tkey in sorted(diffs_by_table.keys()):
                        items = diffs_by_table.get(tkey) or []
                        filtered_items = []
                        for it in items:
                            fds = self._filter_diff_fields(tkey, it.get('fields') or [])
                            if not fds:
                                continue
                            if tkey in ('spell', 'spelldescription'):
                                for fd in fds:
                                    fd['before'] = self._expand_spell_refs(to_build, fd.get('before'))
                                    fd['after'] = self._expand_spell_refs(to_build, fd.get('after'))
                            filtered_items.append({'id': it.get('id'), 'action': it.get('action'), 'fields': fds})
                        if not filtered_items:
                            continue
                        parts.append(f"<div style='margin-top:10px'><div style='font-weight:800'>{html.escape(tkey)}</div>")
                        if tkey == 'spelleffect':
                            parts.append("<div class='diff'>")
                            effects = {}
                            for it in filtered_items:
                                kv = {}
                                for fd in it.get('fields') or []:
                                    kv[fd.get('field')] = (fd.get('before'), fd.get('after'))
                                effect_idx = kv.get('EffectIndex', ('', ''))[1] or kv.get('EffectIndex', ('', ''))[0] or ''
                                try:
                                    effect_idx = int(str(effect_idx))
                                except Exception:
                                    effect_idx = ''
                                effects.setdefault(effect_idx, []).append(kv)
                            for effect_idx in sorted(effects.keys(), key=lambda x: (9999 if x == '' else x)):
                                merged = {}
                                for kv in effects[effect_idx]:
                                    merged.update(kv)
                                eff_type = merged.get('Effect', ('', ''))
                                aura = merged.get('EffectAura', ('', ''))
                                bp = merged.get('EffectBasePointsF', ('', ''))
                                if bp == ('', ''):
                                    bp = merged.get('EffectBasePoints', ('', ''))
                                coef = merged.get('EffectBonusCoefficient', ('', ''))
                                if coef == ('', ''):
                                    coef = merged.get('BonusCoefficientFromAP', ('', ''))
                                if coef == ('', ''):
                                    coef = merged.get('Coefficient', ('', ''))
                                pvp = merged.get('PvpMultiplier', ('', ''))

                                label = f"Effect #{effect_idx}" if effect_idx != '' else "Effect"
                                eff_name = ''
                                try:
                                    et = int(str(eff_type[1] or eff_type[0] or 0))
                                except Exception:
                                    et = 0
                                if et == 6:
                                    eff_name = 'Apply Aura'
                                elif et == 2:
                                    eff_name = 'School Damage'
                                elif et == 10:
                                    eff_name = 'Heal'
                                elif et == 42:
                                    eff_name = 'Trigger Spell'
                                suffix = f" {eff_name}" if eff_name else ""
                                aura_part = ''
                                if aura != ('', ''):
                                    aura_part = f" Aura {aura[0]} → {aura[1]}"
                                parts.append(f"<div style='padding:6px 2px'><div class='mono' style='font-weight:700'>{html.escape(label + suffix)}</div>")
                                if aura_part:
                                    parts.append(f"<div class='mono' style='opacity:.9'>{html.escape(aura_part.strip())}</div>")
                                if bp != ('', ''):
                                    parts.append(f"<div class='mono'>Value: {html.escape(str(bp[0]))} → {html.escape(str(bp[1]))}</div>")
                                if coef != ('', ''):
                                    parts.append(f"<div class='mono'>Coefficient: {html.escape(str(coef[0]))} → {html.escape(str(coef[1]))}</div>")
                                if pvp != ('', ''):
                                    parts.append(f"<div class='mono'>Pvp Multiplier: {html.escape(str(pvp[0]))} → {html.escape(str(pvp[1]))}</div>")
                                parts.append("</div>")
                            parts.append("</div>")
                        elif tkey in ('spell', 'spelldescription'):
                            parts.append("<div class='diff'>")
                            for it in filtered_items:
                                for fd in it.get('fields') or []:
                                    parts.append(f"<div style='margin-top:6px'><div class='mono' style='font-weight:700'>{html.escape(fd.get('field') or '')}</div>")
                                    parts.append("<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:6px'>")
                                    parts.append(f"<div><div style='font-size:11px;opacity:.8'>原值</div><div class='mono' style='white-space:pre-wrap'>{html.escape(fd.get('before') or '')}</div></div>")
                                    parts.append(f"<div><div style='font-size:11px;opacity:.8'>新值</div><div class='mono' style='white-space:pre-wrap'>{html.escape(fd.get('after') or '')}</div></div>")
                                    parts.append("</div></div>")
                            parts.append("</div>")
                        else:
                            parts.append("<div class='diff'><table><thead><tr><th style='width:140px'>条目</th><th>字段</th><th>原值</th><th>新值</th></tr></thead><tbody>")
                            for it in filtered_items:
                                rid = it.get('id')
                                action = (it.get('action') or '').strip()
                                label = f"{tkey}#{rid} {action}".strip()
                                for fd in it.get('fields') or []:
                                    parts.append("<tr>")
                                    parts.append(f"<td class='mono'>{html.escape(label)}</td>")
                                    parts.append(f"<td class='mono'>{html.escape(fd.get('field') or '')}</td>")
                                    parts.append(f"<td class='mono'>{html.escape(fd.get('before') or '')}</td>")
                                    parts.append(f"<td class='mono'>{html.escape(fd.get('after') or '')}</td>")
                                    parts.append("</tr>")
                            parts.append("</tbody></table></div>")
                        parts.append("</div>")

                    parts.append("</div>")

        parts.append('</div></body></html>')
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(parts))

        return {'path': rel_path, 'class_count': class_count}
