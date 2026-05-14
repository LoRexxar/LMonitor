import csv
import html
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.models import WowSkillDiffReport, WowSpellEffectSnapshot, WowSpellSnapshot, WowSpellSnapshotState, WowSpecSpellMapSnapshot, WowWagoMonitorState
from utils.log import logger

try:
    from core.glm import GLMClient
except Exception:
    GLMClient = None


class WagoSkillDiffMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.default_branch = 'wow'
        self.locale = str(getattr(settings, 'WAGO_SKILL_DIFF_LOCALE', 'enUS') or 'enUS')
        self.http_timeout = int(getattr(settings, 'WAGO_SKILL_DIFF_TIMEOUT', 30) or 30)
        self._build_versions_cache = {'ts': 0, 'versions': []}
        self._chr_classes_cache = {}
        self._skilllineability_cache = {}
        self._chr_specialization_cache = {}
        self._chr_specialization_meta_cache = {}
        self._specialization_spells_cache = {}
        self._spell_class_options_cache = {}
        self._spellclassset_to_class_cache = {}
        self._spelleffect_by_spell_cache = {}
        self._spellmisc_by_spell_cache = {}
        self._spellradius_cache = {}
        self._spellpower_by_spell_cache = {}
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
        states = list(WowWagoMonitorState.objects.filter(locale=self.locale, is_active=True).order_by('branch', 'id'))
        if not states:
            return self._scan_legacy(url)
        ok = True
        for st in states:
            ok = self._scan_state(st) and ok
        return ok

    def _scan_legacy(self, url):
        raw_branch = (url or '').strip() or (getattr(self.task, 'target', '') or '').strip()
        if raw_branch in {'-', 'auto', 'default'}:
            raw_branch = ''
        branch = raw_branch or self.default_branch
        current_build = self._fetch_current_build(branch)
        if not current_build:
            return True

        last_build = (getattr(self.task, 'flag', '') or '').strip()
        if last_build in {'0', '-', 'none', 'null'}:
            last_build = ''
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
                        'content_html_path': report.get('content_html_path') or '',
                        'changed_tables_json': report.get('changed_tables_json') or '',
                        'spell_count': int(report.get('spell_count') or 0),
                        'class_count': int(report.get('class_count') or 0),
                    }
                )
            except Exception as e:
                logger.warning(f"[WagoSkillDiffMonitor] save WowSkillDiffReport failed: {e}")
        return True

    def _fetch_prev_build(self, branch, current_build):
        versions = []
        cache = getattr(self, '_build_versions_cache', None) or {}
        ts = float(cache.get('ts') or 0)
        now_ts = float(timezone.now().timestamp())
        cached_versions = cache.get('versions') or []
        if isinstance(cached_versions, list) and cached_versions and (now_ts - ts) < 3600:
            versions = cached_versions
        else:
            text = self._http_get_text("https://wago.tools/builds-diff")
            props = self._extract_inertia_props(text or '') if text else {}
            versions = props.get('versions') or []
            if isinstance(versions, list) and versions:
                self._build_versions_cache = {'ts': now_ts, 'versions': versions}

        if not isinstance(versions, list) or not versions:
            return ''
        current_build = (current_build or '').strip()
        if not current_build:
            return ''
        if current_build in versions:
            i = versions.index(current_build)
            if i + 1 < len(versions):
                return versions[i + 1]
        return ''

    def _repair_state_if_needed(self, st, current_build):
        url = (getattr(st, 'wago_diff_url', '') or '').strip()
        if not url:
            return
        if f"to={current_build}&from={current_build}" not in url:
            return
        ext_raw = (getattr(st, 'ext', '') or '').strip()
        try:
            ext = json.loads(ext_raw) if ext_raw else {}
        except Exception:
            ext = {}
        if not isinstance(ext, dict):
            return
        fb = (ext.get('from_build') or '').strip()
        tb = (ext.get('to_build') or '').strip()
        if fb != current_build or tb != current_build:
            return
        prev_build = self._fetch_prev_build((getattr(st, 'branch', '') or '').strip(), current_build)
        if not prev_build or prev_build == current_build:
            return
        ext['from_build'] = prev_build
        st.wago_diff_url = f"https://wago.tools/builds-diff?to={current_build}&from={prev_build}"
        st.ext = json.dumps(ext, ensure_ascii=False)
        st.save(update_fields=['wago_diff_url', 'ext'])

    def _scan_state(self, st):
        now = timezone.now()
        branch = (st.branch or '').strip() or self.default_branch
        current_build = self._fetch_current_build(branch)
        if not current_build:
            st.last_run_at = now
            st.last_run_status = 'failed'
            st.ext = (st.ext or '')
            if len(st.ext) > 5000:
                st.ext = st.ext[:5000]
            st.save(update_fields=['last_run_at', 'last_run_status', 'ext'])
            return False

        st.last_run_at = now
        st.last_run_status = 'success'
        st.save(update_fields=['last_run_at', 'last_run_status'])

        last_build = (st.build or '').strip()
        if not last_build:
            prev_build = self._fetch_prev_build(branch, current_build)
            from_build = prev_build or current_build
            return self._handle_build_change(st, from_build, current_build, is_init=True)
        if last_build == current_build:
            self._repair_state_if_needed(st, current_build)
            return True
        return self._handle_build_change(st, last_build, current_build, is_init=False)

    def _handle_build_change(self, st, from_build, to_build, is_init=False):
        now = timezone.now()
        branch = (st.branch or '').strip() or self.default_branch
        wago_diff_url = f"https://wago.tools/builds-diff?to={to_build}&from={from_build}"
        report = None
        try:
            report = self._generate_report(branch, from_build, to_build)
        except Exception as e:
            st.last_event_at = now
            st.last_event_status = 'failed'
            st.wago_diff_url = wago_diff_url
            st.ext = f"generate_failed: {e}"
            st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
            return False

        if not report or int(report.get('spell_count') or 0) <= 0:
            st.build = to_build
            st.last_event_at = now
            st.last_event_status = 'init_no_class_change' if is_init else 'build_changed_no_class_change'
            st.report_url = ''
            st.wago_diff_url = wago_diff_url
            st.ext = json.dumps({
                'branch': branch,
                'from_build': from_build,
                'to_build': to_build,
                'spell_count': int((report or {}).get('spell_count') or 0),
                'class_count': int((report or {}).get('class_count') or 0),
                'summary_title': (report or {}).get('summary_title') or '',
            }, ensure_ascii=False)
            st.save(update_fields=['build', 'last_event_at', 'last_event_status', 'report_url', 'wago_diff_url', 'ext'])
            return True

        row = None
        try:
            row, _ = WowSkillDiffReport.objects.update_or_create(
                branch=branch,
                locale=self.locale,
                to_build=to_build,
                defaults={
                    'from_build': from_build,
                    'content_md': report.get('content_md') or '',
                    'content_html_path': report.get('content_html_path') or '',
                    'changed_tables_json': report.get('changed_tables_json') or '',
                    'spell_count': int(report.get('spell_count') or 0),
                    'class_count': int(report.get('class_count') or 0),
                }
            )
        except Exception as e:
            st.last_event_at = now
            st.last_event_status = 'failed'
            st.wago_diff_url = wago_diff_url
            st.ext = f"save_report_failed: {e}"
            st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
            return False

        st.build = to_build
        st.last_event_at = now
        st.last_event_status = 'init_has_class_change' if is_init else 'build_changed_has_class_change'
        st.report_url = f"/portal/wow-skill-diff/{row.id}/" if row else ''
        st.wago_diff_url = wago_diff_url
        st.ext = json.dumps({
            'branch': branch,
            'from_build': from_build,
            'to_build': to_build,
            'spell_count': int(report.get('spell_count') or 0),
            'class_count': int(report.get('class_count') or 0),
            'summary_title': report.get('summary_title') or '',
        }, ensure_ascii=False)
        st.save(update_fields=['build', 'last_event_at', 'last_event_status', 'report_url', 'wago_diff_url', 'ext'])
        return True

    def _heuristic_summary_title(self, class_spell_counts, spell_count, changed_tables, samples=None):
        cn = {
            1: '战士',
            2: '圣骑士',
            3: '猎人',
            4: '潜行者',
            5: '牧师',
            6: '死亡骑士',
            7: '萨满',
            8: '法师',
            9: '术士',
            10: '武僧',
            11: '德鲁伊',
            12: '恶魔猎手',
            13: '唤魔师',
        }

        class_part = ''
        class_count = len(class_spell_counts or {})
        if class_count:
            top = sorted(class_spell_counts.items(), key=lambda x: (-int(x[1] or 0), int(x[0] or 0)))
            names = []
            for cid, _ in top[:2]:
                try:
                    cid = int(cid)
                except Exception:
                    continue
                names.append(cn.get(cid, str(cid)))
            if class_count == 1:
                class_part = f"{names[0]}" if names else "职业"
            elif class_count == 2:
                class_part = "、".join(names) if names else "职业"
            else:
                class_part = f"{'、'.join(names)}等{class_count}职业" if names else f"{class_count}职业"

        kind = []
        tables = set([str(x or '').strip() for x in (changed_tables or []) if str(x or '').strip()])
        if tables.intersection({'TraitNode', 'TraitTree', 'TraitDefinition', 'TraitNodeEntry'}):
            kind.append('天赋调整')
        if tables.intersection({'SpellEffect', 'SpellPower', 'SpellMisc', 'SpellAuraOptions', 'SpellCooldowns', 'SpellCastingTimes', 'SpellRange', 'SpellDuration'}):
            kind.append('数值调整')
        if tables.intersection({'Spell', 'SpellDescription'}):
            kind.append('描述调整')
        if tables.intersection({'SpellName'}):
            kind.append('技能更名')
        sample_tags = []
        for line in list(samples or [])[:12]:
            s = str(line or '')
            if '更名' in s:
                sample_tags.append('更名')
            if any(k in s for k in ('冷却', 'cooldown', 'recharge')):
                sample_tags.append('冷却')
            if any(k in s for k in ('伤害', 'damage', 'base', '基础值', '系数')):
                sample_tags.append('伤害')
            if any(k in s for k in ('治疗', 'heal', 'healing')):
                sample_tags.append('治疗')
            if any(k in s for k in ('消耗', 'mana', 'energy', 'rage', 'insanity', 'focus', 'runic')):
                sample_tags.append('资源')
        sample_tags = [x for x in sample_tags if x]
        if sample_tags:
            freq = {}
            for x in sample_tags:
                freq[x] = int(freq.get(x) or 0) + 1
            top_tags = [k for k, _ in sorted(freq.items(), key=lambda x: (-x[1], x[0]))][:2]
            if top_tags:
                kind.insert(0, '、'.join(top_tags) + '调整')
        kind_part = "、".join(kind[:2])
        prefix = class_part or "职业技能"
        if kind_part:
            return f"{prefix}{kind_part}（{spell_count}项）"
        return f"{prefix}更新（{spell_count}项）"

    def _glm_summary_title(self, class_spell_counts, spell_count, changed_tables, samples=None):
        if not GLMClient:
            return ''
        glm = GLMClient()
        if not getattr(glm, 'client', None):
            return ''
        top = sorted((class_spell_counts or {}).items(), key=lambda x: (-int(x[1] or 0), int(x[0] or 0)))[:5]
        payload = {
            'class_spell_counts_top5': top,
            'class_count': len(class_spell_counts or {}),
            'spell_count': int(spell_count or 0),
            'changed_tables': sorted([str(x or '').strip() for x in (changed_tables or []) if str(x or '').strip()])[:20],
            'samples': list(samples or [])[:12],
        }
        msg = "基于以下JSON生成一个中文摘要标题（16-28字），要像新闻标题一样概括“改了什么”，优先利用 samples 里的代表性改动点，不要包含版本号，不要换行：\n" + json.dumps(payload, ensure_ascii=False)
        out = glm.send_message(msg, max_tokens=120)
        if not out:
            return ''
        out = out.strip().splitlines()[0].strip()
        out = re.sub(r'^[#\s:：\-]+', '', out)
        out = re.sub(r'[\s]+', ' ', out).strip()
        if len(out) > 40:
            out = out[:40].strip()
        return out

    def _extract_summary_samples(self, spell_changes, snap_spells, max_samples=10):
        def to_float(x):
            try:
                s = str(x).strip()
                if not s:
                    return None
                s = s.replace('%', '')
                return float(s)
            except Exception:
                return None

        def kw_hint(text):
            t = (text or '').lower()
            hints = []
            if any(k in t for k in ('cooldown', 'recharge')):
                hints.append('冷却')
            if 'damage' in t:
                hints.append('伤害')
            if any(k in t for k in ('healing', 'heal')):
                hints.append('治疗')
            if any(k in t for k in ('mana', 'energy', 'rage', 'insanity', 'focus', 'runic')):
                hints.append('资源')
            if 'range' in t:
                hints.append('射程')
            if 'radius' in t:
                hints.append('半径')
            return '、'.join(hints[:2])

        field_map = {
            'EffectBasePointsF': '基础值',
            'EffectBasePoints': '基础值',
            'EffectBonusCoefficient': '系数',
            'BonusCoefficientFromAP': '系数(AP)',
            'Coefficient': '系数',
            'PvpMultiplier': 'PvP系数',
            'PowerCostPct': '消耗比例',
            'ManaCost': '法力消耗',
            'ManaPerSecond': '每秒法力',
            'PowerPctPerSecond': '每秒消耗比例',
            'Description_lang': '描述',
            'AuraDescription_lang': '光环描述',
            'Name_lang': '名称',
        }

        candidates = []
        for spell_id, entry in (spell_changes or {}).items():
            diffs_by_table = (entry or {}).get('diffs') or {}
            name = ((snap_spells or {}).get(spell_id) or {}).get('name') or str(spell_id)
            best_score = -1
            best_line = ''
            for tkey, items in diffs_by_table.items():
                for it in items or []:
                    fields = it.get('fields') or []
                    meta = it.get('meta') or {}
                    for f in fields:
                        field = f.get('field') or ''
                        bv = f.get('before')
                        av = f.get('after')
                        score = 0
                        label = field_map.get(field, field)
                        if tkey in ('spelldescription', 'spell'):
                            score = 900
                            hint = kw_hint((bv or '') + ' ' + (av or ''))
                            desc = f"{hint}调整" if hint else "描述调整"
                            line = f"{name}：{desc}"
                        elif tkey == 'spellname':
                            score = 800
                            b = (bv or '').strip()
                            a = (av or '').strip()
                            b = b[:24] + ('…' if len(b) > 24 else '')
                            a = a[:24] + ('…' if len(a) > 24 else '')
                            line = f"{name}：更名 {b}→{a}" if b and a else f"{name}：更名"
                        else:
                            fb = to_float(bv)
                            fa = to_float(av)
                            if fb is None or fa is None:
                                continue
                            delta = abs(fa - fb)
                            score = 200 + min(2000, int(delta * 10))
                            eff_idx = meta.get('EffectIndex')
                            if eff_idx is not None and eff_idx != '':
                                line = f"{name}：效果{eff_idx} {label} {str(bv)[:10]}→{str(av)[:10]}"
                            else:
                                line = f"{name}：{label} {str(bv)[:10]}→{str(av)[:10]}"
                        if score > best_score and line:
                            best_score = score
                            best_line = line
            if best_line:
                candidates.append((best_score, best_line))
        candidates.sort(key=lambda x: -x[0])
        out = []
        seen = set()
        for _, line in candidates:
            k = line.split('：', 1)[0].strip()
            if k in seen:
                continue
            seen.add(k)
            out.append(line)
            if len(out) >= int(max_samples or 10):
                break
        return out

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
        snap_spells = {}
        snap_effects = {}
        snap_map_add = set()
        snap_map_del = set()
        now = timezone.now()

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

                if tkey == 'spellname':
                    n = (after or {}).get('Name_lang')
                    if n is not None:
                        snap_spells.setdefault(spell_id, {})
                        snap_spells[spell_id]['name'] = str(n)
                elif tkey == 'spelldescription':
                    d = (after or {}).get('Description_lang')
                    a = (after or {}).get('AuraDescription_lang')
                    snap_spells.setdefault(spell_id, {})
                    if d is not None:
                        snap_spells[spell_id]['description'] = str(d)
                    if a is not None:
                        snap_spells[spell_id]['aura_description'] = str(a)
                elif tkey == 'spelleffect':
                    meta = payload.get('meta') if diffs else {}
                    eff_idx = (meta or {}).get('EffectIndex')
                    if eff_idx is None or eff_idx == '':
                        eff_idx = (after or {}).get('EffectIndex') or (before or {}).get('EffectIndex') or 0
                    try:
                        eff_idx = int(str(eff_idx))
                    except Exception:
                        eff_idx = 0
                    key = (spell_id, eff_idx)
                    e = {}
                    for k, nk in (('Effect', 'effect'), ('EffectAura', 'effect_aura')):
                        v = (meta or {}).get(k)
                        if v is None or v == '':
                            v = (after or {}).get(k)
                        if v is None or v == '':
                            v = (before or {}).get(k)
                        try:
                            e[nk] = int(str(v))
                        except Exception:
                            e[nk] = None
                    bp = (after or {}).get('EffectBasePointsF')
                    if bp is None or bp == '':
                        bp = (after or {}).get('EffectBasePoints')
                    if bp is None or bp == '':
                        bp = (before or {}).get('EffectBasePointsF') or (before or {}).get('EffectBasePoints') or ''
                    coef = (after or {}).get('EffectBonusCoefficient')
                    if coef is None or coef == '':
                        coef = (after or {}).get('BonusCoefficientFromAP')
                    if coef is None or coef == '':
                        coef = (after or {}).get('Coefficient')
                    if coef is None or coef == '':
                        coef = (before or {}).get('EffectBonusCoefficient') or (before or {}).get('BonusCoefficientFromAP') or (before or {}).get('Coefficient') or ''
                    pvp = (after or {}).get('PvpMultiplier')
                    if pvp is None or pvp == '':
                        pvp = (before or {}).get('PvpMultiplier') or ''
                    e['base_points'] = str(bp)
                    e['coefficient'] = str(coef)
                    e['pvp_multiplier'] = str(pvp)
                    snap_effects[key] = e
                elif tkey == 'specializationspells':
                    spec_id = 0
                    for k in ('SpecID', 'ChrSpecializationID', 'SpecializationID'):
                        v = row.get(k)
                        if v is None:
                            continue
                        try:
                            spec_id = int(str(v).strip() or '0')
                            break
                        except Exception:
                            continue
                    if spec_id > 0:
                        if action == 'removed':
                            snap_map_del.add((spec_id, spell_id))
                        else:
                            snap_map_add.add((spec_id, spell_id))

        filtered_spell_changes = {}
        for spell_id, entry in spell_changes.items():
            diffs_by_table = entry.get('diffs') or {}
            visible = False
            for tkey, items in diffs_by_table.items():
                for it in items or []:
                    if self._filter_diff_fields(tkey, it.get('fields') or []):
                        visible = True
                        break
                if visible:
                    break
            if visible:
                filtered_spell_changes[spell_id] = entry
        spell_changes = filtered_spell_changes

        for spell_id in spell_changes.keys():
            specs = spell_to_specs.get(spell_id) or set()
            for spec_id in specs:
                snap_map_add.add((spec_id, spell_id))
            snap_spells.setdefault(spell_id, {})
        missing_name_ids = []
        existing_name_ids = set(
            WowSpellSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=list(spell_changes.keys()))
            .exclude(name="")
            .values_list('spell_id', flat=True)
        )
        for spell_id in spell_changes.keys():
            if (snap_spells.get(spell_id) or {}).get('name'):
                continue
            if spell_id in existing_name_ids:
                continue
            missing_name_ids.append(spell_id)
        if missing_name_ids:
            fetched = self._fetch_spell_names_concurrent(to_build, missing_name_ids)
            for sid, name in fetched.items():
                snap_spells.setdefault(sid, {})
                snap_spells[sid]['name'] = name

        if not spell_changes:
            return None

        if snap_map_del:
            q = Q()
            for spec_id, spell_id in snap_map_del:
                q |= Q(spec_id=spec_id, spell_id=spell_id)
            WowSpecSpellMapSnapshot.objects.filter(branch=branch, locale=self.locale).filter(q).delete()

        if snap_map_add:
            map_objs = []
            for spec_id, spell_id in snap_map_add:
                map_objs.append(
                    WowSpecSpellMapSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spec_id=spec_id,
                        spell_id=spell_id,
                        snapshot_build=to_build,
                        updated_at=now,
                    )
                )
            WowSpecSpellMapSnapshot.objects.bulk_create(
                map_objs,
                update_conflicts=True,
                update_fields=['snapshot_build', 'updated_at'],
            )

        if snap_spells:
            spell_objs = []
            for spell_id, patch in snap_spells.items():
                spell_objs.append(
                    WowSpellSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spell_id=spell_id,
                        name=(patch.get('name') or '')[:255],
                        description=patch.get('description') or '',
                        aura_description=patch.get('aura_description') or '',
                        snapshot_build=to_build,
                        updated_at=now,
                    )
                )
            WowSpellSnapshot.objects.bulk_create(
                spell_objs,
                update_conflicts=True,
                update_fields=['name', 'description', 'aura_description', 'snapshot_build', 'updated_at'],
            )

        if snap_effects:
            eff_objs = []
            for (spell_id, effect_index), patch in snap_effects.items():
                eff_objs.append(
                    WowSpellEffectSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spell_id=spell_id,
                        effect_index=effect_index,
                        effect=patch.get('effect'),
                        effect_aura=patch.get('effect_aura'),
                        base_points=patch.get('base_points') or '',
                        coefficient=patch.get('coefficient') or '',
                        pvp_multiplier=patch.get('pvp_multiplier') or '',
                        snapshot_build=to_build,
                        updated_at=now,
                    )
                )
            WowSpellEffectSnapshot.objects.bulk_create(
                eff_objs,
                update_conflicts=True,
                update_fields=[
                    'effect',
                    'effect_aura',
                    'base_points',
                    'coefficient',
                    'pvp_multiplier',
                    'snapshot_build',
                    'updated_at',
                ],
            )

        WowSpellSnapshotState.objects.update_or_create(
            branch=branch,
            locale=self.locale,
            defaults={'snapshot_build': to_build},
        )

        server_title = self._branch_title(branch)
        class_spell_counts = {}
        for sid in spell_changes.keys():
            specs = spell_to_specs.get(sid) or []
            cids = set()
            for spid in specs:
                cid = spec_to_class.get(spid)
                if cid:
                    cids.add(cid)
            for cid in cids:
                class_spell_counts[cid] = int(class_spell_counts.get(cid) or 0) + 1

        summary_title = ''
        if len(spell_changes) > 0:
            samples = self._extract_summary_samples(spell_changes, snap_spells, max_samples=10)
            summary_title = self._glm_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples) or self._heuristic_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples)
        content_md = f"# {summary_title or (server_title + ' 职业技能变更报告')}\n\n- 版本：{from_build} → {to_build}\n- 技能数：{len(spell_changes)}\n"
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
            'summary_title': summary_title or '',
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
        m = re.search(r'data-page=(?:"([^"]+)"|\'([^\']+)\')', html_text)
        if not m:
            return {}
        try:
            raw = m.group(1) or m.group(2) or ''
            obj = json.loads(html.unescape(raw))
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

    def _fetch_db2_row_by_id_requests(self, table, build, record_id):
        table = (table or '').strip()
        build = (build or '').strip()
        try:
            record_id = int(record_id)
        except Exception:
            return {}
        if not table or not build or record_id <= 0:
            return {}
        url = f"https://wago.tools/db2/{table}?build={build}&locale={self.locale}&filter[ID]=exact:{record_id}"
        try:
            r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            return {}
        if r.status_code != 200:
            return {}
        try:
            text = r.content.decode('utf-8', 'replace')
        except Exception:
            text = r.text or ''
        props = self._extract_inertia_props(text or '')
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

    def _fetch_spell_names_concurrent(self, build, spell_ids):
        spell_ids = [int(x) for x in (spell_ids or []) if int(x) > 0]
        if not spell_ids:
            return {}
        workers = int(getattr(settings, 'WAGO_SPELLNAME_WORKERS', 12) or 12)
        workers = max(1, min(workers, 32))
        out = {}

        def work(spell_id):
            row = self._fetch_db2_row_by_id_requests('SpellName', build, spell_id)
            name = (row.get('Name_lang') or '').strip()
            return spell_id, name

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for spell_id, name in ex.map(work, spell_ids):
                if name:
                    out[spell_id] = name
        return out

    def _expand_spell_refs(self, build, text, depth=0, visited=None):
        if depth >= 4:
            return text or ''
        visited = visited or set()
        s = str(text or '')
        for m in re.findall(r'\$@(spelldesc|spellaura|spellname)(\d+)', s):
            kind, sid = m[0], m[1]
            key = f"{kind}{sid}"
            if key in visited:
                continue
            visited.add(key)
            spell_id = int(sid or 0)
            if kind == 'spellname':
                name_row = self._fetch_db2_row_by_id('spellname', build, spell_id)
                rep = (name_row.get('Name_lang') or '').strip()
            else:
                desc_row = self._fetch_db2_row_by_id('spelldescription', build, spell_id)
                if kind == 'spelldesc':
                    rep = (desc_row.get('Description_lang') or '').strip()
                else:
                    rep = (desc_row.get('AuraDescription_lang') or '').strip()
                if not rep:
                    spell_row = self._fetch_db2_row_by_id('spell', build, spell_id)
                    if kind == 'spelldesc':
                        rep = (spell_row.get('Description_lang') or '').strip()
                    else:
                        rep = (spell_row.get('AuraDescription_lang') or '').strip()
            if rep:
                rep = self._expand_spell_refs(build, rep, depth=depth + 1, visited=visited)
                s = s.replace(f"$@{kind}{sid}", rep)
        return s

    def _fetch_spelleffect_rows_by_spell(self, build, spell_id):
        build = (build or '').strip()
        try:
            spell_id = int(spell_id)
        except Exception:
            return []
        if not build or spell_id <= 0:
            return []
        key = (build, spell_id)
        if key in self._spelleffect_by_spell_cache:
            return self._spelleffect_by_spell_cache.get(key) or []
        url = f"https://wago.tools/db2/SpellEffect?build={build}&locale={self.locale}&filter%5BSpellID%5D=exact%3A{spell_id}"
        try:
            r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            self._spelleffect_by_spell_cache[key] = []
            return []
        if r.status_code != 200:
            self._spelleffect_by_spell_cache[key] = []
            return []
        props = self._extract_inertia_props(r.text or '')
        payload = props.get('data')
        data = payload.get('data') if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        out = data if isinstance(data, list) else []
        self._spelleffect_by_spell_cache[key] = out
        return out

    def _get_spelleffect_row_by_index(self, build, spell_id, effect_index):
        try:
            effect_index = int(effect_index)
        except Exception:
            return {}
        for r in self._fetch_spelleffect_rows_by_spell(build, spell_id):
            try:
                if int(str(r.get('EffectIndex') or 0)) == effect_index:
                    return r
            except Exception:
                continue
        return {}

    def _fetch_spellmisc_by_spellid(self, build, spell_id):
        build = (build or '').strip()
        try:
            spell_id = int(spell_id)
        except Exception:
            return {}
        if not build or spell_id <= 0:
            return {}
        key = (build, spell_id)
        if key in self._spellmisc_by_spell_cache:
            return self._spellmisc_by_spell_cache.get(key) or {}
        url = f"https://wago.tools/db2/SpellMisc?build={build}&locale={self.locale}&filter%5BSpellID%5D=exact%3A{spell_id}"
        try:
            r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            self._spellmisc_by_spell_cache[key] = {}
            return {}
        if r.status_code != 200:
            self._spellmisc_by_spell_cache[key] = {}
            return {}
        props = self._extract_inertia_props(r.text or '')
        payload = props.get('data')
        data = payload.get('data') if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        row = {}
        if isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else {}
        self._spellmisc_by_spell_cache[key] = row or {}
        return row or {}

    def _fmt_duration_seconds(self, ms):
        try:
            f = float(ms)
        except Exception:
            return ''
        if abs(f) < 1e-9:
            return '0 sec'
        sec = f / 1000.0
        if abs(sec - int(sec)) < 1e-9:
            return f"{int(sec)} sec"
        return f"{self._fmt_num(sec)} sec"

    def _fetch_spellradius(self, build, radius_id):
        build = (build or '').strip()
        try:
            radius_id = int(radius_id)
        except Exception:
            return {}
        if not build or radius_id <= 0:
            return {}
        key = (build, radius_id)
        if key in self._spellradius_cache:
            return self._spellradius_cache.get(key) or {}
        row = self._fetch_db2_row_by_id_requests('SpellRadius', build, radius_id)
        self._spellradius_cache[key] = row or {}
        return row or {}

    def _fetch_spellpower_by_spellid(self, build, spell_id):
        build = (build or '').strip()
        try:
            spell_id = int(spell_id)
        except Exception:
            return {}
        if not build or spell_id <= 0:
            return {}
        key = (build, spell_id)
        if key in self._spellpower_by_spell_cache:
            return self._spellpower_by_spell_cache.get(key) or {}
        url = f"https://wago.tools/db2/SpellPower?build={build}&locale={self.locale}&filter%5BSpellID%5D=exact%3A{spell_id}"
        try:
            r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            self._spellpower_by_spell_cache[key] = {}
            return {}
        if r.status_code != 200:
            self._spellpower_by_spell_cache[key] = {}
            return {}
        props = self._extract_inertia_props(r.text or '')
        payload = props.get('data')
        data = payload.get('data') if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        row = {}
        if isinstance(data, list) and data:
            row = data[0] if isinstance(data[0], dict) else {}
        self._spellpower_by_spell_cache[key] = row or {}
        return row or {}

    def _fmt_num(self, v):
        if v is None:
            return ''
        s = str(v).strip()
        if not s:
            return ''
        try:
            f = float(s)
        except Exception:
            return s
        if abs(f - int(f)) < 1e-9:
            return str(int(f))
        out = f"{f:.6f}".rstrip('0').rstrip('.')
        return out

    def _cleanup_tooltip_text(self, s):
        s = str(s or '')
        s = re.sub(r'\$@spellicon\d+', '', s)
        s = re.sub(r'\|c[0-9A-Fa-f]{8}', '', s)
        s = re.sub(r'\|r', '', s, flags=re.I)
        s = re.sub(r'\|C[0-9A-Fa-f]{8}', '', s)
        s = re.sub(r'\|R', '', s)
        s = s.replace('\r\n', '\n').replace('\r', '\n')
        return s

    def _strip_conditionals(self, s):
        s = str(s or '')
        pattern2 = re.compile(r'\$\?[a-zA-Z]\d+\[([^\]]*)\]\[([^\]]*)\]')
        pattern1 = re.compile(r'\$\?[a-zA-Z]\d+\[([^\]]*)\]')
        while True:
            m = pattern2.search(s)
            if not m:
                break
            a = (m.group(1) or '').strip()
            b = (m.group(2) or '').strip()
            rep = a if a else b
            s = s[:m.start()] + rep + s[m.end():]
        while True:
            m = pattern1.search(s)
            if not m:
                break
            rep = (m.group(1) or '').strip()
            s = s[:m.start()] + rep + s[m.end():]
        return s

    def _eval_numeric_expr(self, expr):
        expr = str(expr or '').strip()
        if not expr:
            return None
        if len(expr) > 80:
            return None
        if not re.fullmatch(r'[0-9\.\+\-\*/\(\)\s<>=!]+', expr):
            return None
        try:
            return eval(expr, {"__builtins__": {}}, {})
        except Exception:
            return None

    def _replace_numeric_expressions(self, s):
        s = str(s or '')

        def repl_fmt(m):
            inner = m.group(1) or ''
            dec = m.group(2) or ''
            v = self._eval_numeric_expr(inner)
            if v is None:
                return m.group(0)
            try:
                d = int(dec)
            except Exception:
                d = 0
            try:
                f = float(v)
            except Exception:
                return self._fmt_num(v)
            out = f"{f:.{max(0, min(d, 6))}f}"
            out = out.rstrip('0').rstrip('.') if '.' in out else out
            return out

        def repl(m):
            inner = m.group(1) or ''
            v = self._eval_numeric_expr(inner)
            if v is None:
                return m.group(0)
            return self._fmt_num(v)

        s = re.sub(r'\$\{([^}]+)\}\.(\d+)', repl_fmt, s)
        s = re.sub(r'\$\{([^}]+)\}', repl, s)
        return s

    def _strip_conditionals_with_removed(self, s):
        s = str(s or '')
        removed = []

        def repl2(m):
            cond = (m.group(1) or '').strip()
            a = (m.group(2) or '').strip()
            b = (m.group(3) or '').strip()
            cv = self._eval_numeric_expr(cond)
            if cv is None:
                keep = a
                drop = b
            else:
                keep = a if float(cv) != 0 else b
                drop = b if float(cv) != 0 else a
            if drop.strip():
                removed.append(drop.strip())
            return keep

        def repl1(m):
            cond = (m.group(1) or '').strip()
            a = (m.group(2) or '').strip()
            cv = self._eval_numeric_expr(cond)
            keep = a if (cv is None or float(cv) != 0) else ''
            drop = '' if keep else a
            if drop.strip():
                removed.append(drop.strip())
            return keep

        s = re.sub(r'\$\?([^\[\]]+)\[([^\]]*)\]\[([^\]]*)\]', repl2, s)
        s = re.sub(r'\$\?([^\[\]]+)\[([^\]]*)\]', repl1, s)
        return s, removed

    def _resolve_tooltip_token(self, build, base_spell_id, ref_spell_id, code, n):
        try:
            base_spell_id = int(base_spell_id)
            ref_spell_id = int(ref_spell_id)
            n = int(n)
        except Exception:
            return None
        if n <= 0:
            return None
        effect_index = n - 1
        row = self._get_spelleffect_row_by_index(build, ref_spell_id, effect_index)
        if not row and code in ('s', 'w', 'm'):
            best = None
            best_abs = -1.0
            for r in self._fetch_spelleffect_rows_by_spell(build, ref_spell_id):
                bp = r.get('EffectBasePointsF')
                try:
                    f = abs(float(str(bp)))
                except Exception:
                    continue
                if f > best_abs:
                    best_abs = f
                    best = r
            row = best or {}
        if not row:
            return None
        if code in ('s', 'w', 'm'):
            bp = row.get('EffectBasePointsF')
            bp_s = self._fmt_num(bp)
            if bp_s.startswith('-'):
                bp_s = bp_s[1:]
            if bp_s and bp_s not in ('0', '0.0'):
                return bp_s
            ap = self._fmt_num(row.get('BonusCoefficientFromAP'))
            if ap and ap not in ('0', '0.0'):
                return f"{ap}×AP"
            sp = self._fmt_num(row.get('EffectBonusCoefficient'))
            if sp and sp not in ('0', '0.0'):
                return f"{sp}×SP"
            sp2 = self._fmt_num(row.get('Coefficient'))
            if sp2 and sp2 not in ('0', '0.0'):
                return f"{sp2}×SP"
            return bp_s or '0'
        if code == 't':
            v = row.get('EffectAuraPeriod')
            if v is None or v == '' or str(v).strip() in ('0', '0.0'):
                v = row.get('EffectAmplitude')
            vs = self._fmt_num(v)
            if not vs or vs in ('0', '0.0'):
                return None
            try:
                f = float(vs)
            except Exception:
                return vs
            if f > 100:
                f = f / 1000.0
            return self._fmt_num(f)
        if code == 'd':
            misc = self._fetch_spellmisc_by_spellid(build, ref_spell_id)
            didx = misc.get('DurationIndex')
            if didx is None or didx == '' or str(didx).strip() in ('0', '0.0'):
                return None
            try:
                didx = int(str(didx))
            except Exception:
                return None
            drow = self._fetch_db2_row_by_id('spellduration', build, didx)
            ms = drow.get('Duration')
            if ms is None or ms == '':
                ms = drow.get('MaxDuration')
            out = self._fmt_duration_seconds(ms)
            return out or None
        if code == 'a':
            row = self._get_spelleffect_row_by_index(build, ref_spell_id, effect_index)
            ridx = row.get('EffectRadiusIndex_0')
            if ridx is None or ridx == '' or str(ridx).strip() in ('0', '0.0'):
                ridx = row.get('EffectRadiusIndex_1')
            try:
                ridx = int(str(ridx))
            except Exception:
                return None
            rr = self._fetch_spellradius(build, ridx)
            val = rr.get('RadiusMax')
            if val is None or val == '':
                val = rr.get('Radius')
            out = self._fmt_num(val)
            return out or None
        return None

    def _render_spell_text_plain(self, build, spell_id, text):
        s = self._expand_spell_refs(build, text or '')
        s = self._cleanup_tooltip_text(s)

        def repl_c(m):
            p = self._fetch_spellpower_by_spellid(build, spell_id)
            pct = p.get('PowerCostPct')
            try:
                f = float(str(pct))
            except Exception:
                return '0'
            c = -f * 10000.0
            return self._fmt_num(c) or '0'

        s = re.sub(r'\$c\b', repl_c, s, flags=re.I)

        def repl(m):
            ref_sid = m.group(1)
            code = (m.group(2) or '').lower()
            n = m.group(3)
            if not code:
                return m.group(0)
            rsid = int(ref_sid) if ref_sid else int(spell_id)
            rep = self._resolve_tooltip_token(build, spell_id, rsid, code, n)
            return rep if rep is not None else m.group(0)

        s = re.sub(r'\$(\d+)?([swtmda])(\d+)', repl, s, flags=re.I)

        def repl_d(m):
            ref_sid = m.group(1)
            rsid = int(ref_sid) if ref_sid else int(spell_id)
            misc = self._fetch_spellmisc_by_spellid(build, rsid)
            didx = misc.get('DurationIndex')
            if didx is None or didx == '' or str(didx).strip() in ('0', '0.0'):
                return m.group(0)
            try:
                didx = int(str(didx))
            except Exception:
                return m.group(0)
            drow = self._fetch_db2_row_by_id('spellduration', build, didx)
            ms = drow.get('Duration')
            if ms is None or ms == '':
                ms = drow.get('MaxDuration')
            out = self._fmt_duration_seconds(ms)
            return out or m.group(0)

        s = re.sub(r'\$(\d+)?d\b', repl_d, s, flags=re.I)

        def repl_big_l(m):
            return (m.group(1) or '').strip()

        s = re.sub(r'\$L\w+:([^;\]]*);', repl_big_l, s)
        s = re.sub(r'\$L:([^;\]\s]+);?', repl_big_l, s)

        def repl_l(m):
            body = (m.group(1) or '').strip()
            if ':' in body:
                body = body.split(':')[-1].strip()
            return body

        s = re.sub(r'\$l\w+:([^;\]]*);', repl_l, s)
        s = re.sub(r'\$l:([^;\]\s]+);?', repl_l, s)
        s = self._replace_numeric_expressions(s)
        s, removed = self._strip_conditionals_with_removed(s)
        s = re.sub(r'\s+', ' ', s).strip()
        removed = [re.sub(r'\s+', ' ', x).strip() for x in (removed or []) if str(x or '').strip()]
        return s, removed

    def _render_spell_text_html(self, build, spell_id, text):
        plain, removed = self._render_spell_text_plain(build, spell_id, text)
        out = html.escape(plain or '')
        if removed:
            removed_text = " ".join([f"<span class='del'>已移除：{html.escape(x)}</span>" for x in removed if x])
            if removed_text:
                out = (out + " " + removed_text).strip()
        return out

    def _tokenize_for_diff(self, s):
        s = str(s or '')
        return [x for x in re.findall(r'[A-Za-z0-9_\.%×]+|\s+|.', s) if x]

    def _inline_diff_html(self, before, after):
        before = str(before or '')
        after = str(after or '')
        b = self._tokenize_for_diff(before)
        a = self._tokenize_for_diff(after)
        sm = __import__('difflib').SequenceMatcher(a=b, b=a, autojunk=False)
        ops = sm.get_opcodes()

        groups = []
        cur = None

        def flush():
            nonlocal cur
            if cur:
                groups.append(cur)
            cur = None

        for tag, i1, i2, j1, j2 in ops:
            btxt = ''.join(b[i1:i2])
            atxt = ''.join(a[j1:j2])
            if tag == 'equal':
                if cur and btxt.strip() == '' and atxt.strip() == '' and len(btxt) <= 2 and len(atxt) <= 2:
                    cur['del'] += btxt
                    cur['ins'] += atxt
                    continue
                flush()
                groups.append({'tag': 'equal', 'text': atxt})
                continue

            if not cur:
                cur = {'tag': 'change', 'del': '', 'ins': ''}
            if tag in ('delete', 'replace'):
                cur['del'] += btxt
            if tag in ('insert', 'replace'):
                cur['ins'] += atxt

        flush()

        out = []
        for g in groups:
            if g.get('tag') == 'equal':
                out.append(html.escape(g.get('text') or ''))
                continue
            d = g.get('del') or ''
            i = g.get('ins') or ''
            if d and i:
                out.append(f"<span class='del'>{html.escape(d)}</span>")
                if d and i and (not d[-1].isspace()) and (not i[0].isspace()):
                    out.append(' ')
                out.append(f"<span class='ins'>{html.escape(i)}</span>")
            elif d:
                out.append(f"<span class='del'>{html.escape(d)}</span>")
            else:
                out.append(f"<span class='ins'>{html.escape(i)}</span>")
        return ''.join(out).replace('\\n', ' ')

    def _write_html_report(self, branch, server_title, from_build, to_build, display_from_build, display_to_build, class_names, spec_meta, spell_to_specs, spec_to_class, spell_changes, wowhead_url=''):
        rel_path = f"portal/reports/wow_skill_diff_{branch}_{self.locale}_{to_build.replace('.', '_')}.html"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        name_cache = {}
        spell_ids = sorted(set(int(x) for x in spell_changes.keys()))
        snap_names = {
            int(r['spell_id']): (r.get('name') or '')
            for r in WowSpellSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=spell_ids)
            .exclude(name='')
            .values('spell_id', 'name')
        }
        name_cache.update(snap_names)
        missing = [sid for sid in spell_ids if not (name_cache.get(sid) or '').strip()]
        if missing:
            fetched = self._fetch_spell_names_concurrent(to_build, missing)
            name_cache.update(fetched)

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
        parts.append('body{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:0;padding:16px;line-height:1.55;background:#f1f5f9;color:#0f172a}')
        parts.append('.card{max-width:1200px;margin:0 auto;background:#ffffff;border:1px solid rgba(148,163,184,.35);border-radius:14px;padding:18px}')
        parts.append('.meta{color:#475569;font-size:12px;margin-top:6px;display:flex;flex-wrap:wrap;gap:10px}')
        parts.append('.toc{margin-top:12px;padding:10px 12px;background:#f8fafc;border:1px solid rgba(226,232,240,1);border-radius:12px}')
        parts.append('.toc a{color:#1d4ed8;text-decoration:none}')
        parts.append('.toc a:hover{text-decoration:underline}')
        parts.append('h2{margin:18px 0 8px 0;font-size:18px}')
        parts.append('h3{margin:12px 0 6px 0;font-size:15px;color:#0f172a}')
        parts.append('.spell{margin-top:12px;padding:10px 12px;background:#f8fafc;border:1px solid rgba(226,232,240,1);border-radius:12px}')
        parts.append('.spell-head{display:flex;gap:6px;align-items:flex-start;font-weight:800}')
        parts.append('.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;margin-top:6px;flex:0 0 auto}')
        parts.append('.spell-title{font-weight:800}')
        parts.append('.subtle{color:#64748b;font-size:12px;font-weight:500}')
        parts.append('.line{margin-top:6px;color:#0f172a;font-size:13px}')
        parts.append('.hash{color:#2563eb;font-weight:800;margin-right:4px}')
        parts.append('.k{color:#334155;font-weight:800}')
        parts.append(".ins{color:#16a34a;font-weight:800}")
        parts.append(".del{color:#dc2626;font-weight:800;text-decoration:line-through}")
        parts.append('.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}')
        parts.append('</style>')
        parts.append('</head>')
        parts.append('<body>')
        parts.append('<div class="card">')
        parts.append(f"<h1 style='margin:0;font-size:18px'>{html.escape(server_title)} 职业技能变更报告：{html.escape(title_from)} → {html.escape(title_to)}</h1>")
        parts.append(f"<div class='meta'><span>技能数：{len(spell_changes)}</span><span>职业数：{class_count}</span><span>语言：{html.escape(self.locale)}</span></div>")
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

        table_title = {
            'spelleffect': '技能效果',
            'spell': '技能描述',
            'spelldescription': '技能描述',
            'spellname': '技能名称',
            'spellmisc': '技能杂项',
            'spellauraoptions': '光环选项',
            'spellinterrupts': '打断/中断',
            'spelltargetrestrictions': '目标限制',
            'spellclassoptions': '职业选项',
            'spellpower': '资源消耗',
            'spellprocsperminute': '每分钟触发',
            'spellcastingtimes': '施法时间',
            'spellranges': '距离',
            'spellduration': '持续时间',
            'spellradius': '半径',
        }
        field_title = {
            'Description_lang': '描述',
            'AuraDescription_lang': '光环描述',
            'Name_lang': '名称',
            'EffectBasePointsF': '基础数值F',
            'EffectBasePoints': '基础数值',
            'EffectBonusCoefficient': '法强系数',
            'BonusCoefficientFromAP': '攻强系数',
            'Coefficient': '系数',
            'PvpMultiplier': 'PvP系数',
            'EffectAmplitude': '周期',
            'EffectAuraPeriod': '周期',
            'SpellID': '技能ID',
            'DifficultyID': '难度ID',
            'RangeIndex': '距离索引',
            'DurationIndex': '持续时间索引',
            'PvPDurationIndex': 'PvP持续时间索引',
            'CastingTimeIndex': '施法时间索引',
            'ContentTuningID': '内容调优ID',
            'LaunchDelay': '发射延迟',
            'MinDuration': '最短持续时间',
            'SchoolMask': '法术系别',
            'ShowFutureSpellPlayerConditionID': '条件ID',
            'Speed': '速度',
            'ProcCategoryRecovery': '触发类别恢复',
            'CumulativeAura': '可叠加光环',
            'ProcChance': '触发几率',
            'ProcCharges': '触发次数',
            'ProcTypeMask_0': '触发类型掩码0',
            'ProcTypeMask_1': '触发类型掩码1',
            'SpellProcsPerMinuteID': 'PPM索引',
            'AuraInterruptFlags_0': '光环中断标记0',
            'AuraInterruptFlags_1': '光环中断标记1',
            'ChannelInterruptFlags_0': '引导中断标记0',
            'ChannelInterruptFlags_1': '引导中断标记1',
            'InterruptFlags': '中断标记',
        }

        def fmt_change(b, a):
            b = '' if b is None else str(b)
            a = '' if a is None else str(a)
            if b == a:
                return html.escape(a)
            return f"<span class='del'>{html.escape(b)}</span> → <span class='ins'>{html.escape(a)}</span>"

        def fmt_removed(removed):
            removed = [x for x in (removed or []) if str(x or '').strip()]
            if not removed:
                return ''
            return ' ' + ' '.join([f"<span class='del'>{html.escape(x)}</span>" for x in removed])

        for cid in sorted(class_to_spec_to_spells.keys()):
            cname = (class_names or {}).get(cid) or str(cid)
            parts.append(f"<h2 id='class-{cid}'>{html.escape(cname)} <span class='subtle'>职业 {cid}</span></h2>")
            spec_map = class_to_spec_to_spells.get(cid) or {}
            for spec_id in sorted(spec_map.keys()):
                if spec_id == 0:
                    spec_name = '通用'
                else:
                    spec_name = ((spec_meta or {}).get(spec_id) or {}).get('name') or str(spec_id)
                parts.append(f"<h3 id='class-{cid}-spec-{spec_id}'>{html.escape(spec_name)} <span class='subtle'>专精 {spec_id}</span></h3>")
                for spell_id in sorted(spec_map.get(spec_id) or []):
                    sname = (name_cache.get(spell_id) or '').strip() or str(spell_id)
                    wowhead_spell_url = f"https://www.wowhead.com/spell={spell_id}"
                    diffs_by_table = (spell_changes.get(spell_id) or {}).get('diffs') or {}
                    if not diffs_by_table:
                        continue
                    desc_primary = ''
                    lines = []

                    for tkey in sorted(diffs_by_table.keys()):
                        items = diffs_by_table.get(tkey) or []
                        filtered_items = []
                        for it in items:
                            fds = self._filter_diff_fields(tkey, it.get('fields') or [])
                            if not fds:
                                continue
                            filtered_items.append({'id': it.get('id'), 'action': it.get('action'), 'fields': fds, 'meta': it.get('meta') or {}})
                        if not filtered_items:
                            continue

                        if tkey == 'spelleffect':
                            effects = {}
                            for it in filtered_items:
                                kv = {}
                                for fd in it.get('fields') or []:
                                    kv[fd.get('field')] = (fd.get('before'), fd.get('after'))
                                meta = it.get('meta') or {}
                                for k in ('EffectIndex', 'Effect', 'EffectAura'):
                                    mv = meta.get(k)
                                    if mv is not None and mv != '' and k not in kv:
                                        kv[k] = (mv, mv)
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
                                try:
                                    et = int(str(eff_type[1] or eff_type[0] or 0))
                                except Exception:
                                    et = 0
                                eff_cn = ''
                                if et == 6:
                                    eff_cn = '应用光环'
                                elif et == 2:
                                    eff_cn = '法术伤害'
                                elif et == 10:
                                    eff_cn = '治疗'
                                elif et == 42:
                                    eff_cn = '触发法术'
                                idx_part = f"(#{effect_idx})" if effect_idx != '' else ''
                                changes = []
                                for fk in ('EffectBasePointsF', 'EffectBasePoints', 'EffectBonusCoefficient', 'BonusCoefficientFromAP', 'Coefficient', 'PvpMultiplier', 'EffectAuraPeriod', 'EffectAmplitude'):
                                    if fk not in merged:
                                        continue
                                    b, a = merged.get(fk) or ('', '')
                                    if str(b) == str(a):
                                        continue
                                    changes.append(f"{field_title.get(fk, fk)}：{fmt_change(b, a)}")
                                if changes:
                                    label = f"{eff_cn}{idx_part}" if eff_cn else f"技能效果{idx_part}"
                                    lines.append(f"<div class='line'><span class='hash'>#</span>{html.escape(label)}（{'，'.join(changes)}）</div>")
                            continue

                        if tkey in ('spell', 'spelldescription'):
                            for it in filtered_items:
                                for fd in it.get('fields') or []:
                                    btxt, b_removed = self._render_spell_text_plain(from_build, spell_id, fd.get('before'))
                                    atxt, a_removed = self._render_spell_text_plain(to_build, spell_id, fd.get('after'))
                                    merged = self._inline_diff_html(btxt, atxt) + fmt_removed(a_removed)
                                    f = fd.get('field') or ''
                                    title = field_title.get(f, f) or '描述'
                                    if not desc_primary and f in ('Description_lang', 'AuraDescription_lang'):
                                        desc_primary = merged
                                    else:
                                        lines.append(f"<div class='line'><span class='k'>{html.escape(table_title.get(tkey, tkey))}</span> {html.escape(title)}：{merged}</div>")
                            continue

                        title = table_title.get(tkey, tkey)
                        for it in filtered_items:
                            for fd in it.get('fields') or []:
                                f = fd.get('field') or ''
                                lines.append(f"<div class='line'><span class='k'>{html.escape(title)}</span> {html.escape(field_title.get(f, f))}：{fmt_change(fd.get('before'), fd.get('after'))}</div>")

                    parts.append(f"<div class='spell' id='spell-{spell_id}'>")
                    parts.append(f"<div class='spell-head'><span class='dot'></span><div><span class='spell-title'>{html.escape(sname)}({spell_id})</span>：{desc_primary} <a class='subtle' href='{html.escape(wowhead_spell_url)}' target='_blank' rel='noopener noreferrer'>Wowhead</a></div></div>")
                    for ln in lines:
                        parts.append(ln)
                    parts.append("</div>")
        parts.append('</div></body></html>')
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(parts))

        return {'path': rel_path, 'class_count': class_count}
