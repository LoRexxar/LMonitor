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
        rows = self._parse_first_table_rows(resp)
        for r in rows:
            b = (r.get('Branch') or '').strip()
            if b == branch:
                return (r.get('Build') or '').strip()
        return ''

    def _generate_report(self, branch, from_build, to_build):
        changed_tables = self._fetch_changed_db2_tables(from_build, to_build)
        if not changed_tables:
            return None

        relevant_tables = []
        for t in sorted(changed_tables):
            lt = t.lower()
            if lt.startswith('spell') or lt == 'skilllineability':
                relevant_tables.append(t)
        if not relevant_tables:
            return None

        class_names = self._load_chr_classes(to_build)
        spell_classmask = self._load_skilllineability_spell_classmask(to_build)
        if not spell_classmask:
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
                if not spell_classmask.get(spell_id):
                    continue
                entry = spell_changes.setdefault(spell_id, {'tables': set(), 'diffs': {}})
                entry['tables'].add(tkey)
                record_id = self._extract_record_id(tkey, row, spell_id)
                if record_id is None:
                    continue
                fields = whitelist.get(tkey) or []
                if not fields:
                    continue
                before = self._fetch_db2_row_by_id(t, from_build, record_id)
                after = self._fetch_db2_row_by_id(t, to_build, record_id)
                diffs = self._diff_fields(fields, before, after)
                if diffs:
                    entry['diffs'].setdefault(tkey, []).append({'id': record_id, 'fields': diffs})

        if not spell_changes:
            return None

        name_cache = {}

        lines = []
        branch_title = self._branch_title(branch)
        lines.append(f"# {branch_title} 职业技能变更报告：{from_build} → {to_build}")
        lines.append("")
        lines.append(f"- 生成时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 变更表：{', '.join(sorted(set([x.lower() for x in relevant_tables])))}")
        lines.append(f"- 变更技能数：{len(spell_changes)}")
        lines.append("")

        class_to_spells = {}
        for spell_id in spell_changes.keys():
            for cid in self._class_ids_from_mask(spell_classmask.get(spell_id, 0)):
                class_to_spells.setdefault(cid, set()).add(spell_id)

        class_count = len(class_to_spells)
        for cid in sorted(class_to_spells.keys()):
            cname = class_names.get(cid) or str(cid)
            lines.append(f"## {cname} ({cid})")
            lines.append("")
            for spell_id in sorted(class_to_spells[cid]):
                sname = name_cache.get(spell_id)
                if sname is None:
                    sname = self._fetch_spell_name(to_build, spell_id) or str(spell_id)
                    name_cache[spell_id] = sname
                lines.append(f"### {sname} ({spell_id})")
                tables = sorted(spell_changes[spell_id]['tables'])
                lines.append(f"- 变更来源：{', '.join(tables)}")
                diffs_by_table = spell_changes[spell_id]['diffs']
                for tkey in tables:
                    if tkey not in diffs_by_table:
                        continue
                    for item in diffs_by_table[tkey]:
                        lines.append(f"- {tkey}#{item.get('id')}")
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
            'wow': 'Retail',
            'wow_beta': 'Beta',
            'wowt': 'PTR',
            'wowxptr': 'PTR X',
        }
        return m.get(branch, branch)

    def _fetch_changed_db2_tables(self, from_build, to_build):
        url = f"https://wago.tools/builds-diff?to={to_build}&from={from_build}"
        text = self._http_get_text(url)
        if not text:
            return set()
        tables = set()
        for m in re.finditer(r"/db2/([a-zA-Z0-9_]+)/diff\\?from=", text):
            tables.add(m.group(1))
        return tables

    def _fetch_db2_diff_rows(self, table, from_build, to_build):
        url = f"https://wago.tools/db2/{table}/diff?from={from_build}&to={to_build}"
        text = self._http_get_text(url)
        if not text:
            return []
        rows = self._parse_first_table_rows(text)
        return rows

    def _load_skilllineability_spell_classmask(self, build):
        url = f"https://wago.tools/db2/SkillLineAbility/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
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
        return m

    def _load_chr_classes(self, build):
        url = f"https://wago.tools/db2/ChrClasses/csv?build={build}&locale={self.locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
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
        return out

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
        rows = self._parse_first_table_rows(text)
        return rows[0] if rows else {}

    def _field_whitelist(self):
        return {
            'spellname': ['Name_lang'],
            'spelldescription': ['Description_lang', 'AuraDescription_lang'],
            'spelleffect': [
                'Effect',
                'EffectAura',
                'EffectBasePoints',
                'EffectBonusCoefficient',
                'EffectAmplitude',
                'EffectAuraPeriod',
                'EffectMiscValue_0',
                'EffectMiscValue_1',
                'ImplicitTarget_0',
                'ImplicitTarget_1',
            ],
            'spellmisc': [],
            'skilllineability': ['SkillLine', 'ClassMask', 'MinSkillLineRank', 'Flags'],
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
