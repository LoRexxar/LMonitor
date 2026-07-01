import csv
import html
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.db.models import Q
from django.db.utils import NotSupportedError
from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.models import WowSkillDiffReport, WowHotfixReport, WowSpellEffectSnapshot, WowSpellSnapshot, WowSpellSnapshotState, WowSpecSpellMapSnapshot, WowWagoBuildEvent, WowWagoHotfixEvent, WowWagoMonitorState
from utils.log import logger
from botend.controller.plugins.wow.wago_regions import wago_region_id, wago_region_name
from botend.services.wago_db2.client import WagoDB2Client
from botend.services.wago_db2.graph import WagoDB2GraphService

try:
    from core.glm import GLMClient
except Exception:
    GLMClient = None


class WagoDiffUnavailable(RuntimeError):
    """Raised when Wago has exposed a build before diff pages are ready."""


class WagoSkillDiffMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.default_branch = 'wow'
        self.locale = str(getattr(settings, 'WAGO_SKILL_DIFF_LOCALE', 'enUS') or 'enUS')
        self.name_locale = 'zhCN'
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
        self._http_session = requests.Session()
        self._http_session.headers.update({
            'User-Agent': 'Mozilla/5.0',
            'Connection': 'close',
        })
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

    def _mark_event(self, event, **fields):
        if not event:
            return
        for key, value in fields.items():
            if key == 'error_message' and value:
                value = str(value)[:4000]
            setattr(event, key, value)
        update_fields = list(fields.keys()) + ['updated_at']
        event.save(update_fields=update_fields)

    def _bulk_upsert_snapshots(self, model, objs, unique_fields, update_fields, batch_size=None):
        if not objs:
            return
        try:
            model.objects.bulk_create(
                objs,
                batch_size=batch_size,
                update_conflicts=True,
                unique_fields=unique_fields,
                update_fields=update_fields,
            )
            return
        except (NotSupportedError, ValueError):
            pass

        key_to_obj = {tuple(getattr(obj, field) for field in unique_fields): obj for obj in objs}
        q = Q()
        for key in key_to_obj.keys():
            item_q = Q()
            for field, value in zip(unique_fields, key):
                item_q &= Q(**{field: value})
            q |= item_q

        existing_by_key = {}
        if q:
            for existing in model.objects.filter(q):
                existing_by_key[tuple(getattr(existing, field) for field in unique_fields)] = existing

        create_objs = []
        update_objs = []
        for key, obj in key_to_obj.items():
            existing = existing_by_key.get(key)
            if not existing:
                create_objs.append(obj)
                continue
            for field in update_fields:
                setattr(existing, field, getattr(obj, field))
            update_objs.append(existing)

        if create_objs:
            model.objects.bulk_create(create_objs, batch_size=batch_size)
        if update_objs:
            model.objects.bulk_update(update_objs, update_fields, batch_size=batch_size)

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
                        'display_from_build': report.get('display_from_build') or '',
                        'display_to_build': report.get('display_to_build') or '',
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

        pending_ok = self._process_pending_build_events(st, limit=1)

        last_build = (st.build or '').strip()
        discovered_build = self._latest_discovered_build(st) or last_build
        if not last_build and not discovered_build:
            prev_build = self._fetch_prev_build(branch, current_build)
            from_build = prev_build or current_build
            self._record_build_event(st, from_build, current_build, is_init=True)
            return pending_ok

        if discovered_build and discovered_build != current_build:
            self._record_build_event(st, discovered_build, current_build, is_init=not bool(last_build))
            return pending_ok

        if last_build == current_build:
            self._repair_state_if_needed(st, current_build)
            self._scan_hotfix_if_needed(st, branch, current_build)
            return pending_ok

        return pending_ok

    def _latest_discovered_build(self, st):
        branch = (st.branch or '').strip() or self.default_branch
        event = WowWagoBuildEvent.objects.filter(
            branch=branch,
            locale=self.locale,
        ).order_by('-detected_at', '-id').first()
        return (event.to_build or '').strip() if event else ''

    def _record_build_event(self, st, from_build, to_build, is_init=False):
        now = timezone.now()
        branch = (st.branch or '').strip() or self.default_branch
        from_build = (from_build or '').strip()
        to_build = (to_build or '').strip()
        if not from_build or not to_build:
            return None

        wago_diff_url = f"https://wago.tools/builds-diff?to={to_build}&from={from_build}"
        event, created = WowWagoBuildEvent.objects.get_or_create(
            branch=branch,
            locale=self.locale,
            from_build=from_build,
            to_build=to_build,
            defaults={
                'status': 'detected',
                'wago_diff_url': wago_diff_url,
                'error_message': '',
            },
        )
        if not created and event.wago_diff_url != wago_diff_url:
            event.wago_diff_url = wago_diff_url
            event.save(update_fields=['wago_diff_url', 'updated_at'])

        st.last_event_at = now
        st.last_event_status = 'init_build_detected' if is_init else 'build_detected'
        st.wago_diff_url = wago_diff_url
        st.ext = json.dumps({
            'branch': branch,
            'from_build': from_build,
            'to_build': to_build,
            'status': event.status or 'detected',
            'event_id': event.id,
            'processing': 'queued',
        }, ensure_ascii=False)
        st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
        return event

    def _process_pending_build_events(self, st, limit=1):
        branch = (st.branch or '').strip() or self.default_branch
        retryable = ['detected', 'diff_unavailable', 'generate_failed', 'save_report_failed', 'rerun_failed']
        qs = WowWagoBuildEvent.objects.filter(
            branch=branch,
            locale=self.locale,
            status__in=retryable,
        ).order_by('detected_at', 'id')
        last_build = (st.build or '').strip()
        if last_build:
            qs = qs.filter(from_build=last_build)
        ok = True
        count = 0
        for event in qs[:max(1, int(limit or 1))]:
            ok = self._process_build_event(st, event) and ok
            count += 1
        return ok if count else True

    def _scan_hotfix_if_needed(self, st, branch, current_build):
        if (branch or '').strip().lower() != 'wow':
            return True
        now = timezone.now()
        hotfix_locale = 'enUS'
        last_push = self._to_int(getattr(st, 'hotfix_push_id', 0) or 0)
        latest_push = self._fetch_latest_hotfix_push_id(locale=hotfix_locale)
        st.hotfix_last_run_at = now
        st.hotfix_last_run_status = 'success' if latest_push > 0 else 'no_data'
        st.save(update_fields=['hotfix_last_run_at', 'hotfix_last_run_status'])
        if latest_push <= 0:
            return True
        if last_push > latest_push:
            logger.warning(
                "[WagoSkillDiffMonitor] hotfix cursor is ahead of Wago latest push, "
                "recover as initial scan: branch=%s locale=%s last_push=%s latest_push=%s",
                branch,
                hotfix_locale,
                last_push,
                latest_push,
            )
            st.hotfix_push_id = 0
            st.hotfix_last_event_status = 'hotfix_cursor_recovered'
            st.save(update_fields=['hotfix_push_id', 'hotfix_last_event_status'])
            last_push = 0
        if latest_push <= last_push:
            return True

        report = None
        is_init = last_push <= 0
        from_push = last_push
        if is_init:
            prev_push = self._fetch_prev_hotfix_push_id(latest_push, locale=hotfix_locale)
            from_push = prev_push if prev_push > 0 else max(0, latest_push - 1)

        hotfix_wago_url = self._hotfix_url(push_id=latest_push, locale=hotfix_locale)
        hotfix_event, _ = WowWagoHotfixEvent.objects.update_or_create(
            branch=branch,
            locale=hotfix_locale,
            to_push=latest_push,
            defaults={
                'from_push': from_push,
                'push_id': latest_push,
                'status': 'detected',
                'wago_url': hotfix_wago_url,
                'error_message': '',
                'last_attempt_at': now,
            },
        )
        fallback_status = ''
        fallback_error = ''
        try:
            report = self._generate_hotfix_full_report(branch, current_build, from_push, latest_push, locale=hotfix_locale)
        except Exception as e:
            fallback_status = 'generate_failed_fallback_report'
            fallback_error = str(e)
            report = self._build_hotfix_fallback_report(
                branch=branch,
                current_build=current_build,
                from_push=from_push,
                to_push=latest_push,
                locale=hotfix_locale,
                reason=f'Hotfix 明细报告生成失败，已生成 fallback 报告：{e}',
            )

        if not report or int(report.get('entry_count') or 0) <= 0:
            fallback_status = fallback_status or 'no_data_fallback_report'
            report = self._build_hotfix_fallback_report(
                branch=branch,
                current_build=current_build,
                from_push=from_push,
                to_push=latest_push,
                locale=hotfix_locale,
                reason='Wago 已检测到新的 hotfix push，但明细接口暂未返回可汇总数据，已生成 fallback 报告。',
                source_report=report,
            )

        row = None
        try:
            row, _ = WowHotfixReport.objects.update_or_create(
                branch=branch,
                locale=hotfix_locale,
                to_push=int(report.get('to_push') or latest_push),
                defaults={
                    'from_push': int(report.get('from_push') or from_push),
                    'build_num': report.get('build_num') or '',
                    'build_str': report.get('build_str') or '',
                    'summary_title': (report.get('summary_title') or '')[:255],
                    'content_md': report.get('content_md') or '',
                    'content_html_path': report.get('content_html_path') or '',
                    'report_url': report.get('report_url') or '',
                    'wago_url': report.get('wago_url') or '',
                    'changed_tables_json': report.get('changed_tables_json') or '',
                    'table_count': int(report.get('table_count') or 0),
                    'entry_count': int(report.get('entry_count') or 0),
                }
            )
        except Exception as e:
            self._mark_event(hotfix_event, status='save_report_failed', report=None, error_message=e)
            st.hotfix_last_event_at = now
            st.hotfix_last_event_status = 'failed'
            st.hotfix_report_url = ''
            st.hotfix_wago_url = hotfix_wago_url
            st.hotfix_spell_count = 0
            st.hotfix_class_count = 0
            st.hotfix_summary_title = ''
            st.save(
                update_fields=[
                    'hotfix_last_event_at',
                    'hotfix_last_event_status',
                    'hotfix_report_url',
                    'hotfix_wago_url',
                    'hotfix_spell_count',
                    'hotfix_class_count',
                    'hotfix_summary_title',
                ]
            )
            return False

        self._mark_event(
            hotfix_event,
            status=fallback_status or 'report_generated',
            report=row,
            build_num=report.get('build_num') or '',
            build_str=report.get('build_str') or '',
            table_count=int(report.get('table_count') or 0),
            entry_count=int(report.get('entry_count') or 0),
            changed_tables_json=report.get('changed_tables_json') or '',
            summary_title=(report.get('summary_title') or '')[:255],
            error_message=fallback_error,
        )

        # 完整报告成功落库后才推进 push 游标；fallback 报告只保证 Dashboard 有可追溯报告，
        # 但保留下一轮自动重试完整明细报告的机会。WowHotfixReport 使用 update_or_create，
        # 因此重复 fallback/retry 会覆盖同一个 to_push 报告，不会堆重复记录。
        state_update_fields = [
            'hotfix_last_event_at',
            'hotfix_last_event_status',
            'hotfix_report_url',
            'hotfix_wago_url',
            'hotfix_spell_count',
            'hotfix_class_count',
            'hotfix_summary_title',
        ]
        if not fallback_status:
            st.hotfix_push_id = latest_push
            state_update_fields.insert(0, 'hotfix_push_id')
        st.hotfix_last_event_at = now
        st.hotfix_last_event_status = (
            ('init_has_update_fallback' if is_init else 'has_update_fallback')
            if fallback_status else
            ('init_has_update' if is_init else 'has_update')
        )
        st.hotfix_report_url = (report.get('report_url') or '') if report else ''
        st.hotfix_wago_url = self._hotfix_url(push_id=latest_push, locale=hotfix_locale)
        # 兼容旧字段：保留 0，避免误导（全量信息请看 WowHotfixReport 表）
        st.hotfix_spell_count = 0
        st.hotfix_class_count = 0
        st.hotfix_summary_title = (report.get('summary_title') or '')[:255]
        st.save(update_fields=state_update_fields)
        return True

    def _hotfix_day_key(self, dt):
        try:
            d = timezone.localtime(dt).date()
        except Exception:
            d = (dt or timezone.now()).date()
        return d.strftime('%Y%m%d')

    def _reset_hotfix_daily_state(self, branch, day_key):
        p = self._hotfix_daily_state_fullpath(branch, day_key)
        if not p:
            return False
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            return False
        return True

    def _hotfix_daily_state_fullpath(self, branch, day_key):
        rel_path = f"portal/reports/wow_hotfix_state_{branch}_{self.locale}_{day_key}.json"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        return full_path

    def _hotfix_url(self, build_num='', push_id=0, locale=''):
        locale = (locale or '').strip()
        build_num = str(build_num or '').strip()
        params = []
        if build_num:
            params.append(f"filter%5Bbuild%5D={build_num}")
        if push_id:
            params.append(f"filter%5Bpush_id%5D={int(push_id)}")
        if locale:
            params.append(f"filter%5Blocale%5D={locale}")
        qs = '&'.join(params)
        return f"https://wago.tools/hotfixes?{qs}" if qs else 'https://wago.tools/hotfixes'

    def _build_hotfix_fallback_report(
        self,
        *,
        branch,
        current_build,
        from_push,
        to_push,
        locale='',
        reason='',
        source_report=None,
    ):
        """
        Wago 已确认出现新 push，但明细抓取/汇总暂时不可用时，仍生成一份可追溯报告。

        这样 Dashboard 不会只看到状态变化却没有报告链接；报告里明确标记这是 fallback，
        并保留 Wago 原始筛选链接，后续可人工复核或由下一轮监控重新生成更完整内容。
        """
        locale = (locale or '').strip() or self.locale
        from_push = int(from_push or 0)
        to_push = int(to_push or 0)
        current_build = str(current_build or '').strip()
        source_report = source_report if isinstance(source_report, dict) else {}
        wago_url = self._hotfix_url(push_id=to_push, locale=locale)
        summary_title = f"Hotfix 更新已检测：push {from_push}→{to_push}（fallback 报告）"
        reason = str(reason or '').strip() or 'Wago 已检测到新的 hotfix push，但暂时无法生成完整明细报告。'
        now_text = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M:%S %Z')
        table_count = int(source_report.get('table_count') or 0)
        entry_count = int(source_report.get('entry_count') or 0)
        changed_tables_json = source_report.get('changed_tables_json') or '[]'

        md_lines = [
            f"# {summary_title}",
            "",
            f"- 分支：{branch}",
            f"- 区域/语言：{locale}",
            f"- Build：{current_build}",
            f"- Push：{from_push} → {to_push}",
            f"- 生成时间：{now_text}",
            f"- Wago：{wago_url}",
            "",
            "## 状态说明",
            "",
            reason,
            "",
            "这说明监控已经确认 hotfix push 有更新；若当前报告缺少表级明细，优先以 Wago 链接作为原始数据源复核。",
        ]
        content_md = "\n".join(md_lines).strip() + "\n"

        rel_path = f"portal/reports/wow_hotfix_fallback_{branch}_{locale}_{to_push}.html"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        def esc(v):
            return html.escape(str(v or ''))

        html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{esc(summary_title)}</title>
  <style>
    body {{ font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,'Noto Sans',sans-serif; margin: 24px; color:#0f172a; }}
    a {{ color:#2563eb; }}
    .box {{ border:1px solid #fde68a; background:#fffbeb; border-radius:8px; padding:14px 16px; margin:16px 0; }}
    .meta {{ color:#475569; line-height:1.8; }}
    code {{ background:#f1f5f9; padding:1px 6px; border-radius:4px; }}
  </style>
</head>
<body>
  <h1>{esc(summary_title)}</h1>
  <div class="meta">
    <div>分支：{esc(branch)} ｜ 区域/语言：{esc(locale)} ｜ Build：{esc(current_build)} ｜ Push：{from_push} → {to_push}</div>
    <div>生成时间：{esc(now_text)}</div>
    <div>Wago 链接：<a href="{esc(wago_url)}" target="_blank" rel="noreferrer">{esc(wago_url)}</a></div>
  </div>
  <div class="box">
    <h2>Fallback 状态说明</h2>
    <p>{esc(reason)}</p>
    <p>监控已确认 hotfix push 有更新，因此仍生成本报告并记录本次事件；如果明细为空，请优先打开上方 Wago 链接复核原始更新。</p>
  </div>
  <h2>当前可用统计</h2>
  <ul>
    <li>表数量：<code>{table_count}</code></li>
    <li>记录数量：<code>{entry_count}</code></li>
  </ul>
</body>
</html>
"""
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(html_text)
            content_html_path = rel_path
            report_url = f"/portal/reports/{rel_path[len('portal/reports/'):] if rel_path.startswith('portal/reports/') else rel_path}"
        except Exception:
            content_html_path = ''
            report_url = ''

        return {
            'branch': branch,
            'locale': locale,
            'build_num': current_build,
            'build_str': current_build,
            'from_push': from_push,
            'to_push': to_push,
            'summary_title': summary_title,
            'content_md': content_md,
            'content_html_path': content_html_path,
            'report_url': report_url,
            'wago_url': wago_url,
            'changed_tables_json': changed_tables_json,
            'table_count': table_count,
            'entry_count': entry_count,
        }

    def _extract_build_number(self, build_str):
        build_str = str(build_str or '').strip()
        if not build_str:
            return ''
        parts = [x.strip() for x in build_str.split('.') if x.strip()]
        if not parts:
            return ''
        last = parts[-1]
        return last if last.isdigit() else ''

    def _to_int(self, v, default=0):
        try:
            return int(str(v).strip() or str(default))
        except Exception:
            return int(default or 0)

    def _load_state_ext(self, st):
        raw = getattr(st, 'ext', '') or ''
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _fetch_latest_hotfix_push_id(self, *, locale=''):
        max_pages = int(getattr(settings, 'WAGO_HOTFIX_MAX_PAGES', 8) or 8)
        locale = (locale or '').strip()
        search = locale
        latest = 0
        page = 1
        while page <= max_pages:
            raw = self._fetch_hotfix_page_data(page=page, search=search) or []
            if not raw:
                break
            for row in raw:
                pid = self._to_int((row or {}).get('push_id') or 0)
                if pid > latest:
                    latest = pid
            if latest > 0:
                break
            page += 1
        return latest

    def _fetch_prev_hotfix_push_id(self, latest_push, *, locale=''):
        latest_push = self._to_int(latest_push or 0)
        if latest_push <= 0:
            return 0
        max_pages = int(getattr(settings, 'WAGO_HOTFIX_MAX_PAGES', 8) or 8)
        locale = (locale or '').strip()
        search = locale
        seen = set()
        found = []
        page = 1
        while page <= max_pages and len(found) < 2:
            raw = self._fetch_hotfix_page_data(page=page, search=search) or []
            if not raw:
                break
            for r in raw:
                pid = self._to_int((r or {}).get('push_id') or 0)
                if pid <= 0 or pid in seen:
                    continue
                seen.add(pid)
                found.append(pid)
                if len(found) >= 2:
                    break
            page += 1
        if not found:
            return 0
        found = sorted(set(found), reverse=True)
        if len(found) >= 2 and found[0] == latest_push:
            return found[1]
        for pid in found:
            if pid < latest_push:
                return pid
        return 0

    _chrome_driver = None

    def _get_chrome_driver(self):
        if self.__class__._chrome_driver is not None:
            return self.__class__._chrome_driver
        try:
            from core.chromeheadless import ChromeDriver
            self.__class__._chrome_driver = ChromeDriver()
        except Exception as e:
            logger.warning(f"[WagoSkillDiff] ChromeDriver init failed: {e}")
            self.__class__._chrome_driver = False
        return self.__class__._chrome_driver

    def _fetch_hotfix_page_data_via_browser(self, search='', page=1):
        try:
            page = int(page or 1)
        except Exception:
            page = 1
        page = max(1, page)
        search = (search or '').strip()

        driver_obj = self._get_chrome_driver()
        if not driver_obj:
            return []

        params = []
        if search:
            params.append(f"search={search}")
        if page > 1:
            params.append(f"page={page}")
        qs = '&'.join(params)
        url = f"https://wago.tools/hotfixes?{qs}" if qs else "https://wago.tools/hotfixes"

        try:
            rows = self._browser_fetch_with_stability(driver_obj, url, search)
            return rows
        except Exception as e:
            logger.warning(f"[WagoSkillDiff] browser fetch failed: {e}")
            try:
                driver_obj.close_driver()
            except Exception:
                pass
            self.__class__._chrome_driver = None
            try:
                driver_obj2 = self._get_chrome_driver()
                if not driver_obj2:
                    return []
                return self._browser_fetch_with_stability(driver_obj2, url, search)
            except Exception as e2:
                logger.warning(f"[WagoSkillDiff] browser retry also failed: {e2}")
                return []

    def _browser_fetch_with_stability(self, driver_obj, url, search=''):
        import time as _t
        driver_obj.driver.get(url)
        driver_obj.driver.wait.load_start()
        try:
            driver_obj.driver.wait.eles_loaded('css:td', timeout=15)
        except Exception:
            pass

        prev_count = -1
        stable = 0
        deadline = _t.time() + 15
        while _t.time() < deadline:
            try:
                trs = driver_obj.driver.eles('css:tbody tr', timeout=1)
                count = len(trs) if trs else 0
                locale_ok = True
                if search and count > 0:
                    try:
                        first_locale = trs[0].eles('css:td')[6].text if len(trs[0].eles('css:td')) > 6 else ''
                        locale_ok = (first_locale or '').strip().lower() == search.lower()
                    except Exception:
                        locale_ok = False
                if count > 0 and count == prev_count and locale_ok:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                prev_count = count
            except Exception:
                stable = 0
            _t.sleep(0.5)

        rows = []
        trs = driver_obj.driver.eles('css:tbody tr', timeout=5)
        for tr in (trs or []):
            tds = tr.eles('css:td')
            if len(tds) < 8:
                continue
            push_id = self._to_int(tds[0].text.strip() if tds[0].text else 0)
            table_name = (tds[1].text or '').strip()
            record_id = self._to_int(tds[2].text.strip() if tds[2].text else 0)
            build = (tds[3].text or '').strip()
            status = (tds[4].text or '').strip()
            region_raw = (tds[5].text or '').strip()
            locale = (tds[6].text or '').strip()
            first_seen_at = (tds[7].text or '').strip()

            row_region_id = 0
            region_name = ''
            if region_raw.isdigit():
                row_region_id = int(region_raw)
            elif region_raw:
                region_name = region_raw
                try:
                    row_region_id = wago_region_id(region_raw)
                except Exception:
                    row_region_id = 0

            rows.append({
                'push_id': push_id,
                'table_name': table_name,
                'record_id': record_id,
                'build': build,
                'status': status,
                'region_id': row_region_id,
                'region': region_name or (str(row_region_id) if row_region_id > 0 else ''),
                'locale': locale,
                'first_seen_at': first_seen_at,
            })
        return rows

    def _fetch_hotfix_page_data(self, build_num='', page=1, *, search=''):
        search = (search or '').strip()
        if search:
            # 优先走浏览器（对部分反爬更稳）；但如果 Chrome 不可用/抓取失败，要有 HTTP 兜底，
            # 否则 hotfix 流程会出现 “latest_push=0” 从而导致状态异常。
            rows = self._fetch_hotfix_page_data_via_browser(search=search, page=page) or []
            if rows:
                return rows
        build_num = str(build_num or '').strip()
        try:
            page = int(page or 1)
        except Exception:
            page = 1
        page = max(1, page)
        # OpenAI / Inertia props 页面解析（无需 Chrome）
        if build_num and search:
            url = f"https://wago.tools/hotfixes?filter%5Bbuild%5D={build_num}&filter%5Blocale%5D={search}&page={page}"
        elif build_num:
            url = f"https://wago.tools/hotfixes?filter%5Bbuild%5D={build_num}&page={page}"
        elif search:
            url = f"https://wago.tools/hotfixes?filter%5Blocale%5D={search}&page={page}"
        else:
            url = f"https://wago.tools/hotfixes?page={page}"
        text = self._http_get_text(url, timeout=max(60, self.http_timeout))
        if not text:
            return []
        props = self._extract_inertia_props(text)
        payload = props.get('hotfixes') or {}
        data = []
        if isinstance(payload, dict):
            data = payload.get('data') or []
        return data if isinstance(data, list) else []

    def _fetch_hotfix_rows(self, build_num='', page=1, push_id=0, *, locale=''):
        locale = (locale or '').strip()
        push_id = self._to_int(push_id or 0)
        search = locale
        raw = self._fetch_hotfix_page_data(build_num, page=page, search=search) or []
        if not raw:
            return []
        out = []
        for r in raw:
            if push_id > 0 and self._to_int((r or {}).get('push_id') or 0) != push_id:
                continue
            out.append(r)
        return out

    def _load_hotfix_daily_state(self, branch, day_key):
        p = self._hotfix_daily_state_fullpath(branch, day_key)
        if not p or not os.path.exists(p):
            return {}
        try:
            with open(p, 'r', encoding='utf-8') as f:
                obj = json.loads(f.read() or '{}')
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _save_hotfix_daily_state(self, branch, day_key, state):
        p = self._hotfix_daily_state_fullpath(branch, day_key)
        if not p:
            return False
        try:
            with open(p, 'w', encoding='utf-8') as f:
                f.write(json.dumps(state or {}, ensure_ascii=False))
        except Exception:
            return False
        return True

    def _generate_hotfix_daily_report(self, branch, current_build, build_num, day_key, base_from_push, delta_from_push, delta_to_push):
        state = self._load_hotfix_daily_state(branch, day_key)
        delta = self._generate_hotfix_delta(
            branch=branch,
            current_build=current_build,
            build_num=build_num,
            from_push=delta_from_push,
            to_push=delta_to_push,
        )
        if not delta:
            return None

        merged = self._merge_hotfix_daily_state(
            state=state,
            branch=branch,
            current_build=current_build,
            day_key=day_key,
            base_from_push=base_from_push,
            to_push=delta_to_push,
            delta=delta,
        )
        self._save_hotfix_daily_state(branch, day_key, merged)

        spell_changes = self._build_spell_changes_from_hotfix_state(merged)
        if not spell_changes:
            return None

        class_names = self._load_chr_classes(current_build)
        spec_meta = self._load_chr_specialization_meta(current_build)
        spec_to_class = {sid: meta.get('class_id') for sid, meta in (spec_meta or {}).items() if meta.get('class_id')}
        spell_to_specs = self._load_specialization_spells(current_build)
        if not spec_to_class or not spell_to_specs:
            return None

        snap_spells = {}
        spell_ids = sorted(set(int(x) for x in spell_changes.keys()))
        for r in WowSpellSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=spell_ids).values('spell_id', 'name', 'description', 'aura_description'):
            sid = int(r.get('spell_id') or 0)
            if sid <= 0:
                continue
            snap_spells[sid] = {
                'name': r.get('name') or '',
                'description': r.get('description') or '',
                'aura_description': r.get('aura_description') or '',
            }

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

        changed_tables = set((merged or {}).get('changed_tables') or [])
        summary_title = ''
        if len(spell_changes) > 0:
            zh_name_lookup = self._ensure_spell_names_zh(branch, current_build, list(spell_changes.keys()))
            samples = self._extract_summary_samples(spell_changes, snap_spells, max_samples=10, name_lookup=zh_name_lookup)
            summary_title = self._glm_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples) or self._heuristic_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples)

        from_push_id = int((merged or {}).get('from_push_id') or base_from_push or 0)
        to_push_id = int((merged or {}).get('to_push_id') or delta_to_push or 0)
        display_from = f"{current_build} Hotfix({day_key})#{from_push_id}"
        display_to = f"{current_build} Hotfix({day_key})#{to_push_id}"
        from_build_key = f"{current_build}-hfd{day_key}-from{from_push_id}"
        to_build_key = f"{current_build}-hfd{day_key}"

        content_md = f"# {summary_title or (server_title + ' 职业技能变更报告')}\n\n- 版本：{display_from} → {display_to}\n- 技能数：{len(spell_changes)}\n"
        html_meta = self._write_html_report(
            branch=branch,
            server_title=server_title,
            from_build=from_build_key,
            to_build=to_build_key,
            display_from_build=display_from,
            display_to_build=display_to,
            class_names=class_names,
            spec_meta=spec_meta,
            spell_to_specs=spell_to_specs,
            spec_to_class=spec_to_class,
            spell_changes=spell_changes,
            data_build=current_build,
        )
        content_html_path = (html_meta or {}).get('path') or ''
        class_count = int((html_meta or {}).get('class_count') or 0)
        return {
            'summary_title': summary_title or '',
            'from_build': from_build_key,
            'to_build': to_build_key,
            'display_from_build': display_from,
            'display_to_build': display_to,
            'content_md': content_md,
            'content_html_path': content_html_path or '',
            'changed_tables_json': json.dumps(sorted(changed_tables), ensure_ascii=False),
            'spell_count': len(spell_changes),
            'class_count': class_count,
            'from_push_id': from_push_id,
            'to_push_id': to_push_id,
        }

    def _merge_hotfix_daily_state(self, state, branch, current_build, day_key, base_from_push, to_push, delta):
        out = state if isinstance(state, dict) else {}
        out['branch'] = branch
        out['locale'] = self.locale
        out['day_key'] = day_key
        out['current_build'] = current_build
        out['from_push_id'] = int(out.get('from_push_id') or base_from_push or 0)
        out['to_push_id'] = int(to_push or 0)
        out['changed_tables'] = sorted(set((out.get('changed_tables') or []) + (delta.get('changed_tables') or [])))
        spells = out.get('spells') or {}
        if not isinstance(spells, dict):
            spells = {}
        delta_spells = delta.get('spells') or {}
        if not isinstance(delta_spells, dict):
            delta_spells = {}
        for sid, entry in delta_spells.items():
            sid = str(sid).strip()
            if not sid:
                continue
            se = spells.get(sid) or {}
            if not isinstance(se, dict):
                se = {}
            sdiffs = se.get('diffs') or {}
            if not isinstance(sdiffs, dict):
                sdiffs = {}
            ediffs = (entry or {}).get('diffs') or {}
            if not isinstance(ediffs, dict):
                ediffs = {}
            for tkey, recs in ediffs.items():
                tkey = (tkey or '').strip().lower()
                if not tkey:
                    continue
                trecs = sdiffs.get(tkey) or {}
                if not isinstance(trecs, dict):
                    trecs = {}
                if not isinstance(recs, dict):
                    recs = {}
                for rid, payload in recs.items():
                    rid = str(rid).strip()
                    if not rid:
                        continue
                    pr = trecs.get(rid) or {}
                    if not isinstance(pr, dict):
                        pr = {}
                    pr['action'] = (payload or {}).get('action') or pr.get('action') or ''
                    if (payload or {}).get('meta') is not None:
                        pr['meta'] = (payload or {}).get('meta')
                    fields = pr.get('fields') or {}
                    if not isinstance(fields, dict):
                        fields = {}
                    pfields = (payload or {}).get('fields') or {}
                    if not isinstance(pfields, dict):
                        pfields = {}
                    for fname, fv in pfields.items():
                        fname = str(fname).strip()
                        if not fname:
                            continue
                        cur = fields.get(fname) or {}
                        if not isinstance(cur, dict):
                            cur = {}
                        if 'before' not in cur:
                            cur['before'] = (fv or {}).get('before') or ''
                        cur['after'] = (fv or {}).get('after') or ''
                        fields[fname] = cur
                    pr['fields'] = fields
                    trecs[rid] = pr
                sdiffs[tkey] = trecs
            se['diffs'] = sdiffs
            spells[sid] = se
        out['spells'] = spells
        return out

    def _build_spell_changes_from_hotfix_state(self, state):
        spells = (state or {}).get('spells') or {}
        if not isinstance(spells, dict):
            return {}
        out = {}
        for sid, se in spells.items():
            try:
                spell_id = int(str(sid).strip() or '0')
            except Exception:
                spell_id = 0
            if spell_id <= 0 or not isinstance(se, dict):
                continue
            diffs_by_table = {}
            tables = set()
            sdiffs = se.get('diffs') or {}
            if not isinstance(sdiffs, dict):
                continue
            for tkey, trecs in sdiffs.items():
                tkey = (tkey or '').strip().lower()
                if not tkey or not isinstance(trecs, dict):
                    continue
                payloads = []
                for rid, pr in trecs.items():
                    if not isinstance(pr, dict):
                        continue
                    try:
                        pid = int(str(rid).strip() or '0')
                    except Exception:
                        pid = 0
                    fields = pr.get('fields') or {}
                    if not isinstance(fields, dict):
                        continue
                    flist = []
                    for fname, fv in fields.items():
                        if not isinstance(fv, dict):
                            continue
                        b = str(fv.get('before') or '')
                        a = str(fv.get('after') or '')
                        if b == a:
                            continue
                        flist.append({'field': fname, 'before': b, 'after': a})
                    flist = self._filter_diff_fields(tkey, flist)
                    if not flist:
                        continue
                    payload = {'id': pid, 'action': (pr.get('action') or '').strip(), 'fields': flist}
                    meta = pr.get('meta')
                    if meta is not None:
                        payload['meta'] = meta
                    payloads.append(payload)
                if payloads:
                    diffs_by_table[tkey] = payloads
                    tables.add(tkey)
            if diffs_by_table:
                out[spell_id] = {'tables': tables, 'diffs': diffs_by_table}
        return out

    def _generate_hotfix_delta(self, branch, current_build, from_push, to_push, *, locale=''):
        max_pages = int(getattr(settings, 'WAGO_HOTFIX_MAX_PAGES', 8) or 8)
        max_pushes = int(getattr(settings, 'WAGO_HOTFIX_MAX_PUSHES', 20) or 20)
        max_entries = int(getattr(settings, 'WAGO_HOTFIX_MAX_ENTRIES', 2000) or 2000)

        hotfix_rows = []
        push_ids = set()
        locale = (locale or '').strip()
        search = locale
        page = 1
        while page <= max_pages and len(hotfix_rows) < max_entries:
            raw = self._fetch_hotfix_page_data(page=page, search=search) or []
            if not raw:
                break
            min_pid = 0
            for r in raw:
                pid = self._to_int((r or {}).get('push_id') or 0)
                if pid <= 0:
                    continue
                if min_pid <= 0 or pid < min_pid:
                    min_pid = pid
                if locale and (str((r or {}).get('locale') or '').strip() or '') != locale:
                    continue
                if pid <= int(from_push or 0):
                    continue
                if int(to_push or 0) > 0 and pid > int(to_push or 0):
                    continue
                hotfix_rows.append(r)
                push_ids.add(pid)
                if len(hotfix_rows) >= max_entries:
                    break
            if min_pid > 0 and min_pid <= int(from_push or 0):
                break
            page += 1

        if not hotfix_rows:
            return None

        push_sorted = sorted(push_ids)
        if max_pushes > 0 and len(push_sorted) > max_pushes:
            keep = set(push_sorted[-max_pushes:])
            hotfix_rows = [r for r in hotfix_rows if self._to_int((r or {}).get('push_id') or 0) in keep]

        spec_meta = self._load_chr_specialization_meta(current_build)
        spec_to_class = {sid: meta.get('class_id') for sid, meta in (spec_meta or {}).items() if meta.get('class_id')}
        spell_to_specs = self._load_specialization_spells(current_build)
        if not spec_to_class or not spell_to_specs:
            return None

        spell_ids = set()
        effect_records = []
        changed_tables = set()
        for r in hotfix_rows:
            tkey = (str((r or {}).get('table_name') or '').strip() or '').lower()
            rid = self._to_int((r or {}).get('record_id') or 0)
            if not tkey or rid <= 0:
                continue
            if tkey not in self.core_tables:
                continue
            if tkey == 'spelleffect':
                row = self._fetch_db2_row_by_id('SpellEffect', current_build, rid)
                sid = self._extract_spell_id('spelleffect', row)
                if sid <= 0:
                    continue
                try:
                    eff_idx = int(str((row or {}).get('EffectIndex') or '0').strip() or '0')
                except Exception:
                    eff_idx = 0
                effect_records.append({'id': rid, 'spell_id': sid, 'effect_index': eff_idx})
                spell_ids.add(sid)
            else:
                spell_ids.add(rid)
            changed_tables.add(tkey)

        spell_ids = sorted(set(int(x) for x in spell_ids if int(x) > 0))
        if not spell_ids:
            return None

        filtered_spell_ids = []
        for sid in spell_ids:
            if self._spell_has_class(sid, spell_to_specs, spec_to_class, current_build):
                filtered_spell_ids.append(sid)
        spell_ids = filtered_spell_ids
        if not spell_ids:
            return None

        snap_spells = {}
        for sid in spell_ids:
            snap_spells[sid] = {}

        existing_spell_rows = {
            int(r.spell_id): r
            for r in WowSpellSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=spell_ids)
        }
        existing_eff_rows = {
            (int(r.spell_id), int(r.effect_index)): r
            for r in WowSpellEffectSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=spell_ids)
        }

        for sid in spell_ids:
            row = self._fetch_db2_row_by_id('SpellName', current_build, sid)
            n = (row.get('Name_lang') if isinstance(row, dict) else None)
            if n is not None:
                snap_spells.setdefault(sid, {})
                snap_spells[sid]['name'] = str(n)
            row = self._fetch_db2_row_by_id('SpellDescription', current_build, sid)
            if isinstance(row, dict):
                d = row.get('Description_lang')
                a = row.get('AuraDescription_lang')
                if d is not None:
                    snap_spells.setdefault(sid, {})
                    snap_spells[sid]['description'] = str(d)
                if a is not None:
                    snap_spells.setdefault(sid, {})
                    snap_spells[sid]['aura_description'] = str(a)

        snap_effects = {}
        for rec in effect_records:
            sid = int(rec.get('spell_id') or 0)
            if sid <= 0 or sid not in set(spell_ids):
                continue
            rid = int(rec.get('id') or 0)
            if rid <= 0:
                continue
            row = self._fetch_db2_row_by_id('SpellEffect', current_build, rid)
            if not isinstance(row, dict):
                continue
            try:
                eff_idx = int(str((row or {}).get('EffectIndex') or '0').strip() or '0')
            except Exception:
                eff_idx = int(rec.get('effect_index') or 0)
            key = (sid, int(eff_idx or 0))
            e = {}
            for k, nk in (('Effect', 'effect'), ('EffectAura', 'effect_aura')):
                try:
                    e[nk] = int(str((row or {}).get(k) or '').strip() or '0')
                except Exception:
                    e[nk] = None
            bp = (row or {}).get('EffectBasePointsF')
            if bp is None or bp == '':
                bp = (row or {}).get('EffectBasePoints')
            coef = (row or {}).get('EffectBonusCoefficient')
            if coef is None or coef == '':
                coef = (row or {}).get('BonusCoefficientFromAP')
            if coef is None or coef == '':
                coef = (row or {}).get('Coefficient')
            pvp = (row or {}).get('PvpMultiplier')
            e['base_points'] = str(bp if bp is not None else '')
            e['coefficient'] = str(coef if coef is not None else '')
            e['pvp_multiplier'] = str(pvp if pvp is not None else '')
            snap_effects[key] = e

        spells = {}
        now = timezone.now()
        for sid in spell_ids:
            before = existing_spell_rows.get(sid)
            after = snap_spells.get(sid) or {}
            diffs = {}
            if 'name' in after:
                b = (before.name if before else '') or ''
                a = after.get('name') or ''
                flist = self._filter_diff_fields('spellname', [{'field': 'Name_lang', 'before': str(b), 'after': str(a)}])
                if flist:
                    diffs.setdefault('spellname', {}).setdefault(str(sid), {'action': 'changed', 'fields': {}})
                    for f in flist:
                        diffs['spellname'][str(sid)]['fields'][f.get('field')] = {'before': f.get('before') or '', 'after': f.get('after') or ''}
            for fk, label in (('description', 'Description_lang'), ('aura_description', 'AuraDescription_lang')):
                if fk in after:
                    b = (getattr(before, fk) if before else '') or ''
                    a = after.get(fk) or ''
                    flist = self._filter_diff_fields('spelldescription', [{'field': label, 'before': str(b), 'after': str(a)}])
                    if flist:
                        diffs.setdefault('spelldescription', {}).setdefault(str(sid), {'action': 'changed', 'fields': {}})
                        for f in flist:
                            diffs['spelldescription'][str(sid)]['fields'][f.get('field')] = {'before': f.get('before') or '', 'after': f.get('after') or ''}

            for (esid, eff_idx), patch in snap_effects.items():
                if esid != sid:
                    continue
                before_eff = existing_eff_rows.get((sid, eff_idx))
                fields = [
                    {'field': 'Effect', 'before': str(before_eff.effect) if before_eff and before_eff.effect is not None else '', 'after': str(patch.get('effect') if patch.get('effect') is not None else '')},
                    {'field': 'EffectAura', 'before': str(before_eff.effect_aura) if before_eff and before_eff.effect_aura is not None else '', 'after': str(patch.get('effect_aura') if patch.get('effect_aura') is not None else '')},
                    {'field': 'EffectBasePointsF', 'before': str((before_eff.base_points if before_eff else '') or ''), 'after': str(patch.get('base_points') or '')},
                    {'field': 'EffectBonusCoefficient', 'before': str((before_eff.coefficient if before_eff else '') or ''), 'after': str(patch.get('coefficient') or '')},
                    {'field': 'PvpMultiplier', 'before': str((before_eff.pvp_multiplier if before_eff else '') or ''), 'after': str(patch.get('pvp_multiplier') or '')},
                ]
                flist = self._filter_diff_fields('spelleffect', fields)
                if flist:
                    diffs.setdefault('spelleffect', {}).setdefault(str(eff_idx), {'action': 'changed', 'meta': {'EffectIndex': eff_idx}, 'fields': {}})
                    for f in flist:
                        diffs['spelleffect'][str(eff_idx)]['fields'][f.get('field')] = {'before': f.get('before') or '', 'after': f.get('after') or ''}

            if diffs:
                spells[str(sid)] = {'diffs': diffs}

        spell_objs = []
        for sid, patch in snap_spells.items():
            spell_objs.append(
                WowSpellSnapshot(
                    branch=branch,
                    locale=self.locale,
                    spell_id=sid,
                    name=(patch.get('name') or '')[:255],
                    description=patch.get('description') or '',
                    aura_description=patch.get('aura_description') or '',
                    snapshot_build=current_build,
                    updated_at=now,
                )
            )
        if spell_objs:
            self._bulk_upsert_snapshots(
                WowSpellSnapshot,
                spell_objs,
                unique_fields=['branch', 'locale', 'spell_id'],
                update_fields=['name', 'description', 'aura_description', 'snapshot_build', 'updated_at'],
            )

        if snap_effects:
            eff_objs = []
            for (sid, eff_idx), patch in snap_effects.items():
                eff_objs.append(
                    WowSpellEffectSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spell_id=sid,
                        effect_index=eff_idx,
                        effect=patch.get('effect'),
                        effect_aura=patch.get('effect_aura'),
                        base_points=patch.get('base_points') or '',
                        coefficient=patch.get('coefficient') or '',
                        pvp_multiplier=patch.get('pvp_multiplier') or '',
                        snapshot_build=current_build,
                        updated_at=now,
                    )
                )
            self._bulk_upsert_snapshots(
                WowSpellEffectSnapshot,
                eff_objs,
                unique_fields=['branch', 'locale', 'spell_id', 'effect_index'],
                update_fields=['effect', 'effect_aura', 'base_points', 'coefficient', 'pvp_multiplier', 'snapshot_build', 'updated_at'],
            )

        WowSpellSnapshotState.objects.update_or_create(
            branch=branch,
            locale=self.locale,
            defaults={'snapshot_build': current_build},
        )

        if not spells:
            return None

        return {
            'spells': spells,
            'changed_tables': sorted(changed_tables),
        }

    def _handle_build_change(self, st, from_build, to_build, is_init=False):
        """Backward-compatible wrapper: record a build event, then process it once."""
        event = self._record_build_event(st, from_build, to_build, is_init=is_init)
        if not event:
            return False
        return self._process_build_event(st, event, is_init=is_init)

    def _process_build_event(self, st, event, is_init=False):
        now = timezone.now()
        branch = (event.branch or getattr(st, 'branch', '') or '').strip() or self.default_branch
        from_build = (event.from_build or '').strip()
        to_build = (event.to_build or '').strip()
        wago_diff_url = (event.wago_diff_url or '').strip() or f"https://wago.tools/builds-diff?to={to_build}&from={from_build}"

        self._mark_event(event, last_attempt_at=now, error_message='')

        report = None
        try:
            report = self._generate_report(branch, from_build, to_build)
        except Exception as e:
            self._mark_event(event, status='generate_failed', report=None, error_message=e)
            st.last_event_at = now
            st.last_event_status = 'failed'
            st.wago_diff_url = wago_diff_url
            st.ext = f"generate_failed: {e}"
            st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
            return False

        if not report or int(report.get('spell_count') or 0) <= 0:
            if report and report.get('diff_unavailable'):
                err = report.get('error') or 'Wago build diff is not available yet; keep cursor for retry.'
                self._mark_event(
                    event,
                    status='diff_unavailable',
                    report=None,
                    spell_count=0,
                    class_count=0,
                    changed_tables_json=report.get('changed_tables_json') or '',
                    summary_title='',
                    error_message=err,
                )
                st.last_event_at = now
                st.last_event_status = 'diff_unavailable'
                st.wago_diff_url = wago_diff_url
                st.ext = json.dumps({
                    'branch': branch,
                    'from_build': from_build,
                    'to_build': to_build,
                    'status': 'diff_unavailable',
                    'event_id': event.id,
                    'error': err,
                }, ensure_ascii=False)
                st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
                return False

            self._mark_event(
                event,
                status='no_class_change',
                report=None,
                spell_count=int((report or {}).get('spell_count') or 0),
                class_count=int((report or {}).get('class_count') or 0),
                changed_tables_json=(report or {}).get('changed_tables_json') or '',
                summary_title=((report or {}).get('summary_title') or '')[:255],
                error_message='',
            )
            st.build = to_build
            st.last_event_at = now
            st.last_event_status = 'init_no_class_change' if is_init else 'build_changed_no_class_change'
            st.report_url = ''
            st.wago_diff_url = wago_diff_url
            st.ext = json.dumps({
                'branch': branch,
                'from_build': from_build,
                'to_build': to_build,
                'event_id': event.id,
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
                    'display_from_build': report.get('display_from_build') or '',
                    'display_to_build': report.get('display_to_build') or '',
                    'content_md': report.get('content_md') or '',
                    'content_html_path': report.get('content_html_path') or '',
                    'changed_tables_json': report.get('changed_tables_json') or '',
                    'spell_count': int(report.get('spell_count') or 0),
                    'class_count': int(report.get('class_count') or 0),
                }
            )
        except Exception as e:
            self._mark_event(event, status='save_report_failed', report=None, error_message=e)
            st.last_event_at = now
            st.last_event_status = 'failed'
            st.wago_diff_url = wago_diff_url
            st.ext = f"save_report_failed: {e}"
            st.save(update_fields=['last_event_at', 'last_event_status', 'wago_diff_url', 'ext'])
            return False

        self._mark_event(
            event,
            status='report_generated',
            report=row,
            spell_count=int(report.get('spell_count') or 0),
            class_count=int(report.get('class_count') or 0),
            changed_tables_json=report.get('changed_tables_json') or '',
            summary_title=(report.get('summary_title') or '')[:255],
            error_message='',
        )

        st.build = to_build
        st.last_event_at = now
        st.last_event_status = 'init_has_class_change' if is_init else 'build_changed_has_class_change'
        st.report_url = f"/portal/wow-skill-diff/{row.id}/" if row else ''
        st.wago_diff_url = wago_diff_url
        st.ext = json.dumps({
            'branch': branch,
            'from_build': from_build,
            'to_build': to_build,
            'event_id': event.id,
            'spell_count': int(report.get('spell_count') or 0),
            'class_count': int(report.get('class_count') or 0),
            'summary_title': report.get('summary_title') or '',
        }, ensure_ascii=False)
        st.save(update_fields=['build', 'last_event_at', 'last_event_status', 'report_url', 'wago_diff_url', 'ext'])
        return True

    def rerun_build_diff(self, branch, from_build, to_build, locale=None):
        """手动按指定 build 区间重跑 Wago 职业技能差异报告。

        只生成/覆盖 WowSkillDiffReport，不推进 WowWagoMonitorState.build，避免影响自动监控游标。
        """
        branch = (branch or '').strip()
        from_build = (from_build or '').strip()
        to_build = (to_build or '').strip()
        target_locale = (locale or '').strip() or self.locale

        if not branch:
            return {'success': False, 'error': '缺少 branch'}
        if not from_build:
            return {'success': False, 'error': '缺少 from_build'}
        if not to_build:
            return {'success': False, 'error': '缺少 to_build'}
        if from_build == to_build:
            return {'success': False, 'error': 'from_build 和 to_build 不能相同'}

        old_locale = self.locale
        try:
            self.locale = target_locale
            report = self._generate_report(branch, from_build, to_build)
            if report and report.get('diff_unavailable'):
                return {
                    'success': False,
                    'status': 'diff_unavailable',
                    'error': report.get('error') or 'Wago build diff is not available yet',
                    'from_build': from_build,
                    'to_build': to_build,
                    'branch': branch,
                    'locale': target_locale,
                }
            if not report:
                server_title = self._branch_title(branch)
                empty_html = self._write_empty_html_report(
                    branch=branch,
                    server_title=server_title,
                    from_build=from_build,
                    to_build=to_build,
                    display_from_build='',
                    display_to_build='',
                    message='指定版本区间未发现可归属到职业/专精的技能变更。',
                )
                report = {
                    'display_from_build': '',
                    'display_to_build': '',
                    'content_md': '',
                    'content_html_path': (empty_html or {}).get('path') or '',
                    'changed_tables_json': '[]',
                    'spell_count': 0,
                    'class_count': 0,
                }

            row, _ = WowSkillDiffReport.objects.update_or_create(
                branch=branch,
                locale=target_locale,
                to_build=to_build,
                defaults={
                    'from_build': from_build,
                    'display_from_build': report.get('display_from_build') or '',
                    'display_to_build': report.get('display_to_build') or '',
                    'content_md': report.get('content_md') or '',
                    'content_html_path': report.get('content_html_path') or '',
                    'changed_tables_json': report.get('changed_tables_json') or '[]',
                    'spell_count': int(report.get('spell_count') or 0),
                    'class_count': int(report.get('class_count') or 0),
                }
            )
            report_url = f'/portal/wow-skill-diff/{row.id}/'
            return {
                'success': True,
                'report_id': row.id,
                'report_url': report_url,
                'spell_count': int(report.get('spell_count') or 0),
                'class_count': int(report.get('class_count') or 0),
                'from_build': from_build,
                'to_build': to_build,
                'branch': branch,
                'locale': target_locale,
                'message': '指定版本报告已生成' if int(report.get('spell_count') or 0) > 0 else '指定版本报告已生成（无职业技能变更）',
            }
        except Exception as e:
            logger.error(f"[WagoSkillDiffMonitor] manual rerun failed: {e}\n", exc_info=True)
            return {'success': False, 'error': str(e)}
        finally:
            self.locale = old_locale

    def rerun_build_event(self, event_id):
        """按 WowWagoBuildEvent 记录重跑 build diff。

        只更新 event 和 WowSkillDiffReport，不推进 WowWagoMonitorState.build。
        """
        try:
            event_id = int(event_id or 0)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'event_id 无效'}
        if event_id <= 0:
            return {'success': False, 'error': '缺少 event_id'}

        try:
            event = WowWagoBuildEvent.objects.get(id=event_id)
        except WowWagoBuildEvent.DoesNotExist:
            return {'success': False, 'error': f'Wago build event 不存在: {event_id}', 'event_id': event_id}

        now = timezone.now()
        self._mark_event(event, last_attempt_at=now, error_message='')

        result = self.rerun_build_diff(
            branch=event.branch,
            from_build=event.from_build,
            to_build=event.to_build,
            locale=event.locale,
        )

        if not result.get('success'):
            self._mark_event(
                event,
                status='rerun_failed',
                error_message=result.get('error') or 'event 重跑失败',
                last_attempt_at=now,
            )
            result.update({'event_id': event.id, 'status': 'rerun_failed'})
            return result

        report_id = result.get('report_id')
        report_row = None
        if report_id:
            report_row = WowSkillDiffReport.objects.filter(id=report_id).first()

        spell_count = int(result.get('spell_count') or 0)
        status = 'report_generated'
        self._mark_event(
            event,
            status=status,
            report=report_row,
            spell_count=spell_count,
            class_count=int(result.get('class_count') or 0),
            changed_tables_json=(report_row.changed_tables_json if report_row else '') or '[]',
            summary_title='',
            error_message='',
            last_attempt_at=now,
        )

        result.update({
            'event_id': event.id,
            'status': status,
            'report_id': report_id,
            'report_url': result.get('report_url') or (f'/portal/wow-skill-diff/{report_id}/' if report_id else ''),
        })
        return result

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

    def _extract_summary_samples(self, spell_changes, snap_spells, max_samples=10, name_lookup=None):
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
            name = ((name_lookup or {}).get(spell_id) or '').strip() or ((snap_spells or {}).get(spell_id) or {}).get('name') or str(spell_id)
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
        try:
            changed_tables = set(self._fetch_changed_db2_tables(from_build, to_build) or [])
        except WagoDiffUnavailable as e:
            return {
                'diff_unavailable': True,
                'error': str(e),
                'changed_tables_json': '[]',
                'spell_count': 0,
                'class_count': 0,
            }
        relevant_tables = [t for t in sorted(changed_tables) if (t or '').lower() in self.core_tables]
        if not relevant_tables:
            for t in sorted(self.core_tables):
                try:
                    diff_rows = self._fetch_db2_diff_rows(t, from_build, to_build)
                except WagoDiffUnavailable as e:
                    return {
                        'diff_unavailable': True,
                        'error': str(e),
                        'changed_tables_json': json.dumps(sorted(changed_tables), ensure_ascii=False),
                        'spell_count': 0,
                        'class_count': 0,
                    }
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
            self._bulk_upsert_snapshots(
                WowSpecSpellMapSnapshot,
                map_objs,
                unique_fields=['branch', 'locale', 'spec_id', 'spell_id'],
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
            self._bulk_upsert_snapshots(
                WowSpellSnapshot,
                spell_objs,
                unique_fields=['branch', 'locale', 'spell_id'],
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
            self._bulk_upsert_snapshots(
                WowSpellEffectSnapshot,
                eff_objs,
                unique_fields=['branch', 'locale', 'spell_id', 'effect_index'],
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
            zh_name_lookup = self._ensure_spell_names_zh(branch, to_build, list(spell_changes.keys()))
            samples = self._extract_summary_samples(spell_changes, snap_spells, max_samples=10, name_lookup=zh_name_lookup)
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

    def _generate_hotfix_full_report(self, branch, current_build, from_push, to_push, *, locale=''):
        """
        Hotfix 全量更新报告：覆盖所有表的变更（不仅职业/技能）。

        报告产物：
        - content_md：用于存档/检索
        - content_html_path：源码 static/portal/reports 下的报告相对路径
        - report_url：/portal/reports/... 形式的受限映射入口（避免依赖 collectstatic）
        """
        max_pages = int(getattr(settings, 'WAGO_HOTFIX_MAX_PAGES', 8) or 8)
        max_entries = int(getattr(settings, 'WAGO_HOTFIX_MAX_ENTRIES', 4000) or 4000)
        max_sample_per_table = int(getattr(settings, 'WAGO_HOTFIX_REPORT_SAMPLE_PER_TABLE', 20) or 20)
        max_enrich_setting = getattr(settings, 'WAGO_HOTFIX_REPORT_ENRICH_MAX', 50)
        max_enrich = 50 if max_enrich_setting is None else int(max_enrich_setting)

        locale = (locale or '').strip() or self.locale
        search = locale

        hotfix_rows = []
        page = 1
        while page <= max_pages and len(hotfix_rows) < max_entries:
            raw = self._fetch_hotfix_page_data(page=page, search=search) or []
            if not raw:
                break
            for r in raw:
                pid = self._to_int((r or {}).get('push_id') or 0)
                if pid <= 0:
                    continue
                if locale and (str((r or {}).get('locale') or '').strip() or '') != locale:
                    continue
                # (from_push, to_push]
                if pid <= int(from_push or 0):
                    continue
                if int(to_push or 0) > 0 and pid > int(to_push or 0):
                    continue
                hotfix_rows.append(r)
                if len(hotfix_rows) >= max_entries:
                    break
            # Wago hotfix pages are not reliably monotonic when filtered/searched by locale:
            # later pages can still contain rows for a newer push after an older push appears
            # on page 1. Do not stop early based on min push; scan the bounded page window.
            page += 1

        if not hotfix_rows:
            return None

        # build number 取众数（理论上同一次 hotfix 推送应在同一个 build）。
        # 注意：Wago hotfix 列表里的 build 常是短号（如 68275），但 DB2 页面查询
        # 需要完整 build 字符串（如 12.0.7.68367）。报告展示保留短号，DB2 enrich
        # 优先用 current_build，避免查 /db2/...?...build=68275 导致字段展开为空。
        build_counts = {}
        for r in hotfix_rows:
            b = str((r or {}).get('build') or '').strip()
            if not b:
                continue
            build_counts[b] = int(build_counts.get(b) or 0) + 1
        build_num = ''
        if build_counts:
            build_num = sorted(build_counts.items(), key=lambda x: (-x[1], x[0]))[0][0]
        db2_build = str(current_build or '').strip() or build_num

        # group by table
        by_table = {}
        for r in hotfix_rows:
            t = (str((r or {}).get('table_name') or '').strip() or '')
            rid = self._to_int((r or {}).get('record_id') or 0)
            if not t or rid <= 0:
                continue
            by_table.setdefault(t, []).append(r)

        table_stats = [(t, len(rows)) for t, rows in by_table.items()]
        table_stats.sort(key=lambda x: (-x[1], x[0].lower()))

        entry_count = sum(c for _, c in table_stats)
        table_count = len(table_stats)
        wago_url = self._hotfix_url(push_id=int(to_push or 0), locale=locale)
        summary_title = f"Hotfix 全量更新：{table_count} 张表 / {entry_count} 项（push {from_push}→{to_push}）"

        # 生成 markdown（用于存档）
        md_lines = [
            f"# {summary_title}",
            "",
            f"- 分支：{branch}",
            f"- 区域/语言：{locale}",
            f"- Build：{build_num or current_build}",
            f"- Push：{from_push} → {to_push}",
            f"- Wago：{wago_url}",
            "",
            "## 变更概览（按表汇总）",
            "",
            "| DB2表 | 变更数 |",
            "| --- | ---: |",
        ]
        for t, c in table_stats[:200]:
            md_lines.append(f"| {t} | {c} |")
        if len(table_stats) > 200:
            md_lines.append(f"\n> 仅展示前 200 张表，其余 {len(table_stats) - 200} 张表请查看 HTML 报告。")

        md_lines.append("\n## 变更详情（抽样）\n")

        # enrich：尽量把 record_id 显示成可读信息（有 Name/Description 的优先）
        enrich_left = max_enrich

        def _pretty_row_brief(db2_row: dict) -> str:
            if not isinstance(db2_row, dict):
                return ""
            keys = [
                "Name_lang", "Name", "DisplayName_lang", "Title_lang",
                "Description_lang", "Text_lang", "SpellName", "ID",
            ]
            for k in keys:
                v = db2_row.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return ""

        detail_blocks = []
        for t, _ in table_stats:
            rows = by_table.get(t) or []
            # 去重 record_id
            ids = []
            seen = set()
            for r in rows:
                rid = self._to_int((r or {}).get("record_id") or 0)
                if rid <= 0 or rid in seen:
                    continue
                seen.add(rid)
                ids.append(rid)
                if len(ids) >= max_sample_per_table:
                    break

            if not ids:
                continue

            md_lines.append(f"### {t}（{len(rows)}）")
            md_lines.append("")
            for rid in ids:
                brief = ""
                if enrich_left > 0 and build_num:
                    old_locale = getattr(self, 'locale', None)
                    try:
                        if locale and old_locale is not None:
                            self.locale = locale
                        db2_row = self._fetch_db2_row_by_id(t, db2_build, rid)
                        brief = _pretty_row_brief(db2_row)
                        enrich_left -= 1
                    except Exception:
                        brief = ""
                    finally:
                        if old_locale is not None:
                            self.locale = old_locale
                if brief:
                    md_lines.append(f"- `{rid}`：{brief}")
                else:
                    md_lines.append(f"- `{rid}`")
            md_lines.append("")

        content_md = "\n".join(md_lines).strip() + "\n"

        # 输出静态 HTML（Dashboard 打开）
        html_path, html_rel_path = self._write_hotfix_full_html(
            branch=branch,
            locale=locale,
            to_push=int(to_push or 0),
            summary_title=summary_title,
            wago_url=wago_url,
            build_num=build_num or current_build,
            db2_build=db2_build,
            from_push=int(from_push or 0),
            table_stats=table_stats,
            by_table=by_table,
            sample_per_table=max_sample_per_table,
            enrich_max=max_enrich,
        )
        report_url = f"/portal/reports/{html_rel_path[len('portal/reports/'):] if html_rel_path.startswith('portal/reports/') else html_rel_path}" if html_rel_path else ""

        return {
            "branch": branch,
            "locale": locale,
            "build_num": build_num or "",
            "build_str": str(current_build or ""),
            "from_push": int(from_push or 0),
            "to_push": int(to_push or 0),
            "summary_title": summary_title,
            "content_md": content_md,
            "content_html_path": html_rel_path or "",
            "report_url": report_url,
            "wago_url": wago_url,
            "changed_tables_json": json.dumps([t for t, _ in table_stats], ensure_ascii=False),
            "table_count": table_count,
            "entry_count": entry_count,
        }

    def _write_hotfix_full_html(
        self,
        *,
        branch: str,
        locale: str,
        to_push: int,
        summary_title: str,
        wago_url: str,
        build_num: str,
        from_push: int,
        table_stats: list,
        by_table: dict,
        sample_per_table: int,
        enrich_max: int,
        db2_build: str = '',
    ):
        """
        生成 Hotfix 全量静态 HTML 报告（不依赖前端/Portal）。

        Hotfix 本身不像 builds-diff 那样提供完整 before/after payload；这里把 Wago
        返回的 table/record_id 进一步 enrich 成“能读懂的当前记录”：表级影响、类别分组、
        所有表的名称/描述/ID/关联字段/原始字段快照、技能类表的额外技能名摘要和 Wago 原链。
        返回：(full_path, rel_path)
        """
        rel_path = f"portal/reports/wow_hotfix_full_{branch}_{locale}_{to_push}.html"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        build_num = str(build_num or '').strip()
        db2_build = str(db2_build or '').strip() or build_num
        table_stats = list(table_stats or [])
        by_table = by_table or {}
        entry_count = sum(int(c or 0) for _, c in table_stats)
        table_count = len(table_stats)
        sample_per_table = max(1, int(sample_per_table or 20))
        enrich_left = max(0, int(enrich_max or 0))
        row_cache = {}
        spell_name_cache = {}

        category_rules = [
            ('spell', '技能/法术'), ('talent', '天赋'), ('trait', '天赋树'),
            ('item', '物品/装备'), ('journal', '地下城手册'), ('quest', '任务'),
            ('creature', '生物/NPC'), ('map', '地图/区域'), ('area', '区域'),
            ('currency', '货币'), ('achievement', '成就'), ('mount', '坐骑'), ('vehicle', '载具/交互'),
            ('transmog', '幻化'), ('collectable', '收藏'), ('ui', '界面'),
            ('garr', '要塞/追随者'), ('battlepet', '宠物'), ('chr', '角色/职业'),
        ]
        # 这些字段优先放在卡片前面；其余字段仍会继续展示，避免 hotfix 报告只还原技能/天赋。
        readable_fields = [
            'ID', 'Name_lang', 'Name', 'DisplayName_lang', 'Title_lang', 'Description_lang',
            'AuraDescription_lang', 'Text_lang', 'VerifiedBuild', 'SpellID', 'EffectIndex',
            'Effect', 'EffectAura', 'EffectBasePointsF', 'EffectBasePoints',
            'EffectBonusCoefficient', 'BonusCoefficientFromAP', 'Coefficient', 'PvpMultiplier',
            'ClassID', 'SpecID', 'ChrSpecializationID', 'SkillLine', 'SkillLineID',
            'TraitNodeID', 'TraitNodeEntryID', 'TraitDefinitionID', 'ItemID', 'QuestID',
            'CreatureID', 'MapID', 'AreaID', 'CurrencyID', 'AchievementID', 'FileDataID',
            'SourceSpellID', 'CreatureDisplayInfoID', 'SpeciesID', 'SourceTypeEnum', 'IconFileDataID',
            'VehicleID', 'VehicleSeatID', 'Flags', 'FlagsB', 'AttachmentID',
            'CameraEnteringDelay', 'CameraEnteringDuration',
        ]
        field_labels = {
            'ID': '记录 ID', 'Name_lang': '名称', 'Name': '名称', 'DisplayName_lang': '显示名', 'Title_lang': '标题',
            'Description_lang': '描述', 'AuraDescription_lang': '光环描述', 'Text_lang': '文本',
            'VerifiedBuild': '数据 build', 'SpellID': '技能 ID', 'EffectIndex': '效果序号',
            'Effect': '效果类型', 'EffectAura': '光环类型', 'EffectBasePointsF': '基础数值F',
            'EffectBasePoints': '基础数值', 'EffectBonusCoefficient': '法强系数',
            'BonusCoefficientFromAP': '攻强系数', 'Coefficient': '系数', 'PvpMultiplier': 'PvP 系数',
            'ClassID': '职业 ID', 'SpecID': '专精 ID', 'ChrSpecializationID': '专精 ID',
            'SkillLine': '技能线', 'SkillLineID': '技能线', 'TraitNodeID': '天赋节点',
            'TraitNodeEntryID': '天赋节点条目', 'TraitDefinitionID': '天赋定义', 'ItemID': '物品 ID',
            'QuestID': '任务 ID', 'CreatureID': '生物 ID', 'MapID': '地图 ID', 'AreaID': '区域 ID',
            'CurrencyID': '货币 ID', 'AchievementID': '成就 ID', 'FileDataID': '文件 ID',
            'SourceSpellID': '来源技能 ID', 'CreatureDisplayInfoID': '生物外观 ID', 'SpeciesID': '宠物品种 ID',
            'SourceTypeEnum': '来源类型', 'IconFileDataID': '图标文件 ID', 'VehicleID': '载具 ID',
            'VehicleSeatID': '载具座位 ID', 'Flags': '标志位', 'FlagsB': '标志位 B',
            'AttachmentID': '挂点 ID', 'CameraEnteringDelay': '进入相机延迟', 'CameraEnteringDuration': '进入相机时长',
        }
        table_labels = {
            'spellname': '技能名称', 'spelldescription': '技能描述', 'spelleffect': '技能效果',
            'spellscripttext': '技能脚本文本', 'spellmisc': '技能杂项', 'spellpower': '资源消耗', 'spellcooldowns': '冷却',
            'spellduration': '持续时间', 'spellradius': '半径', 'spellrange': '距离',
            'chrspecialization': '专精', 'skilllineability': '技能线关联', 'talent': '天赋',
            'traitnode': '天赋节点', 'traitnodeentry': '天赋节点条目', 'traitdefinition': '天赋定义',
            'modifiertree': '条件/规则树', 'item': '物品', 'itemsparse': '物品文本', 'itemeffect': '物品效果',
            'itemxitemeffect': '物品-效果关联', 'journalencounter': '地下城/团本首领',
            'journalencountersection': '地下城手册文本', 'questv2': '任务', 'questline': '任务线',
            'creature': '生物/NPC', 'creaturedisplayinfo': '生物模型', 'map': '地图', 'areatable': '区域',
            'currencytypes': '货币', 'achievement': '成就', 'mount': '坐骑', 'vehicleseat': '载具座位',
            'battlepetspecies': '战斗宠物品种',
        }
        table_relationships = {
            'spelleffect': 'SpellEffect.SpellID → SpellName.ID；EffectIndex/EffectAura/EffectBasePoints 是技能效果字段',
            'spellname': 'SpellName.ID = SpellID；Name_lang 是技能名称',
            'spelldescription': 'SpellDescription.ID = SpellID；Description_lang 是技能描述',
            'spellscripttext': 'SpellScriptText.ID = record_id；Text_lang 是技能/脚本文本内容',
            'traitnodeentry': 'TraitNodeEntry.TraitDefinitionID → TraitDefinition.ID；通常再关联 SpellID/天赋节点',
            'traitdefinition': 'TraitDefinition.ID；SpellID/OverrideName_lang 指向天赋展示对象',
            'itemeffect': 'ItemEffect.SpellID → SpellName.ID；ItemXItemEffect.ItemEffectID 可反查物品',
            'itemxitemeffect': 'ItemXItemEffect.ItemID → ItemSparse.ID；ItemEffectID → ItemEffect.ID',
            'itemsparse': 'ItemSparse.ID = ItemID；Display_lang/Name_lang 是物品名',
            'questv2clitask': 'QuestV2CliTask.QuestID → QuestV2.ID；ObjectiveText_lang 是任务目标文本',
            'questv2': 'QuestV2.ID = QuestID；Title_lang/Name_lang 是任务名',
            'mount': 'Mount.ID = MountID；Name_lang 是坐骑名；SourceSpellID → SpellName.ID；CreatureDisplayInfoID 是外观',
            'battlepetspecies': 'BattlePetSpecies.ID/SpeciesID；CreatureID 指向生物；SourceTypeEnum 是来源类型',
            'vehicleseat': 'VehicleSeat.ID = 座位记录；VehicleID/AttachmentID/Flags 是载具座位配置字段',
            'modifiertree': 'ModifierTree.ID = 条件节点；Parent/Operator/Amount/Type 字段组成规则树',
        }

        def esc(s):
            return html.escape(str(s or ""))

        def norm_table(t):
            return str(t or '').strip()

        def tkey(t):
            return norm_table(t).lower()

        def table_label(t):
            key = tkey(t)
            zh = table_labels.get(key)
            return f"{zh} / {norm_table(t)}" if zh else norm_table(t)

        def table_relationship(t):
            return table_relationships.get(tkey(t)) or f"{table_label(t)}.ID = record_id；字段含义见当前行字段标签"

        def table_system(t):
            return table_category(t)

        def hotfix_table_url(table_name='', push_id=0):
            params = []
            if push_id:
                params.append(f"filter%5Bpush_id%5D={int(push_id)}")
            if locale:
                params.append(f"filter%5Blocale%5D={locale}")
            if table_name:
                params.append(f"filter%5Btable_name%5D={html.escape(str(table_name), quote=True)}")
            return 'https://wago.tools/hotfixes?' + '&'.join(params) if params else 'https://wago.tools/hotfixes'

        def fetch_row(table_name, record_id):
            nonlocal enrich_left
            key = (norm_table(table_name), int(record_id or 0))
            if key in row_cache:
                return row_cache[key]
            if enrich_left <= 0 or not db2_build or key[1] <= 0:
                row_cache[key] = None
                return None
            old_locale = getattr(self, 'locale', None)
            try:
                if locale and old_locale is not None:
                    self.locale = locale
                row = self._fetch_db2_row_by_id(key[0], db2_build, key[1])
            except Exception:
                row = None
            finally:
                if old_locale is not None:
                    self.locale = old_locale
            enrich_left -= 1
            row_cache[key] = row if isinstance(row, dict) else None
            return row_cache[key]

        def first_text(row):
            if not isinstance(row, dict):
                return ''
            for k in ('Name_lang', 'Name', 'DisplayName_lang', 'Title_lang'):
                v = row.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for k in ('Description_lang', 'AuraDescription_lang', 'Text_lang'):
                v = row.get(k)
                if isinstance(v, str) and v.strip():
                    txt = re.sub(r'\s+', ' ', v.strip())
                    return txt[:220] + ('…' if len(txt) > 220 else '')
            return ''

        def spell_name(spell_id):
            spell_id = self._to_int(spell_id or 0)
            if spell_id <= 0:
                return ''
            if spell_id in spell_name_cache:
                return spell_name_cache[spell_id]
            name = ''
            row = fetch_row('SpellName', spell_id)
            if isinstance(row, dict):
                name = str(row.get('Name_lang') or row.get('Name') or '').strip()
            if not name:
                try:
                    fetched = self._fetch_spell_names_concurrent(db2_build, [spell_id]) if db2_build else {}
                    name = str((fetched or {}).get(spell_id) or '').strip()
                except Exception:
                    name = ''
            spell_name_cache[spell_id] = name
            return name

        def summarize_row(table_name, record_id, row):
            key = tkey(table_name)
            if not isinstance(row, dict):
                return ''
            if key == 'spelleffect':
                sid = self._extract_spell_id('spelleffect', row)
                sname = spell_name(sid)
                idx = row.get('EffectIndex')
                bits = []
                for k in ('Effect', 'EffectAura', 'EffectBasePointsF', 'EffectBasePoints', 'EffectBonusCoefficient', 'BonusCoefficientFromAP', 'Coefficient', 'PvpMultiplier'):
                    v = row.get(k)
                    if v is not None and str(v) != '':
                        bits.append(f"{field_labels.get(k, k)}={v}")
                title = f"{sname or ('Spell ' + str(sid))} / 效果#{idx}" if sid else f"效果记录 {record_id}"
                return title + ("：" + '，'.join(bits[:6]) if bits else '')
            if key in ('spellname', 'spelldescription'):
                sid = record_id
                sname = spell_name(sid)
                brief = first_text(row)
                return f"{sname or ('Spell ' + str(sid))}" + (f"：{brief}" if brief else '')
            if key.startswith('spell'):
                sid = self._extract_spell_id(key, row) or record_id
                sname = spell_name(sid)
                brief = first_text(row)
                return f"{sname or ('Spell ' + str(sid))}" + (f"：{brief}" if brief else '')
            return first_text(row)

        def row_fields_html(table_name, record_id, row):
            semantic_html = (
                "<div class='semantic-fallback'>"
                f"<div class='semantic-line'><span>DB2 表</span><strong>{esc(table_label(table_name))}</strong></div>"
                f"<div class='semantic-line'><span>类别</span><strong>{esc(table_system(table_name))}</strong></div>"
                f"<div class='semantic-line'><span>字段关系</span><strong>{esc(table_relationship(table_name))}</strong></div>"
                f"<div class='semantic-line'><span>追踪键</span><strong>{esc(norm_table(table_name))} #{int(record_id or 0)}</strong></div>"
                "</div>"
            )
            if not isinstance(row, dict):
                return semantic_html + "<div class='muted'>未读取到当前行字段；保留 DB2 表、record_id、push 作为追踪键。</div>"

            key = tkey(table_name)
            text_fields = {
                'Name_lang', 'Name', 'DisplayName_lang', 'Title_lang',
                'Description_lang', 'AuraDescription_lang', 'Text_lang', 'ObjectiveText_lang',
                'OverrideName_lang', 'Display_lang',
            }
            id_fields = {
                'SpellID', 'EffectIndex', 'ClassID', 'SpecID', 'ChrSpecializationID',
                'TraitNodeID', 'TraitNodeEntryID', 'TraitDefinitionID', 'ItemID', 'QuestID',
                'CreatureID', 'MapID', 'AreaID', 'CurrencyID', 'AchievementID', 'SourceSpellID',
                'CreatureDisplayInfoID', 'SpeciesID', 'VehicleID', 'VehicleSeatID',
            }
            metadata_noise_fields = {'ID', 'VerifiedBuild'}
            default_one_fields = {'PvpMultiplier', 'GroupSizeBasePointsCoefficient', 'EffectChainAmplitude'}

            def compact_value(v, max_len=260):
                text = re.sub(r'\s+', ' ', str(v).strip())
                return text[:max_len] + ('…' if len(text) > max_len else '')

            def is_zeroish(v):
                s = str(v).strip()
                if s in ('', '0', '0.0', '0.00', '0.000000', '0.0000000', '0.00000000'):
                    return True
                try:
                    return float(s) == 0.0
                except Exception:
                    return False

            def is_default_noise(field, v):
                if v is None or str(v).strip() == '':
                    return True
                if field in metadata_noise_fields:
                    return True
                if is_zeroish(v):
                    return True
                if field in default_one_fields:
                    try:
                        return float(str(v).strip()) == 1.0
                    except Exception:
                        return str(v).strip() == '1'
                # 大量 Flag/坐标/相机字段只有在非零时才有阅读价值；零值进折叠详情。
                low = field.lower()
                if (low.startswith('flags') or 'camera' in low or 'pos_' in low) and is_zeroish(v):
                    return True
                return False

            ordered_field_names = []
            for k in readable_fields:
                if k in row and k not in ordered_field_names:
                    ordered_field_names.append(k)
            for k in row.keys():
                if k not in ordered_field_names:
                    ordered_field_names.append(k)

            primary_names = []
            for k in ordered_field_names:
                if k not in row:
                    continue
                v = row.get(k)
                if v is None or str(v).strip() == '':
                    continue
                if k in text_fields:
                    primary_names.append(k)
                    continue
                if k in id_fields:
                    primary_names.append(k)
                    continue
                if k in readable_fields and not is_default_noise(k, v):
                    primary_names.append(k)
                    continue
                if k not in readable_fields and not is_default_noise(k, v):
                    # 非白名单字段只展示少量真正有值的字段，避免内部字段淹没正文。
                    primary_names.append(k)

            # 去重并限制主卡片字段数量；完整原始字段放到折叠区。
            seen = set()
            primary_names = [k for k in primary_names if not (k in seen or seen.add(k))]
            if key == 'spelleffect':
                priority = ['SpellID', 'EffectIndex', 'Effect', 'EffectAura', 'EffectBasePointsF', 'EffectBasePoints', 'EffectBonusCoefficient', 'BonusCoefficientFromAP', 'Coefficient', 'PvpMultiplier']
            else:
                priority = list(text_fields) + list(id_fields)
            primary_names = sorted(primary_names, key=lambda k: (priority.index(k) if k in priority else 999, ordered_field_names.index(k) if k in ordered_field_names else 9999))[:14]

            chips = []
            for k in primary_names:
                v = row.get(k)
                text = compact_value(v)
                css = 'field primary' if k in readable_fields or k in text_fields or k in id_fields else 'field raw important'
                chips.append(f"<div class='{css}'><span>{esc(field_labels.get(k, k))}</span><strong>{esc(text)}</strong></div>")

            field_html = "<div class='fields important-fields'>" + ''.join(chips) + "</div>" if chips else "<div class='muted'>当前行没有非默认字段可展示；完整 DB2 原始字段已收起。</div>"

            raw_chips = []
            raw_count = 0
            for k in ordered_field_names:
                if k not in row:
                    continue
                v = row.get(k)
                if v is None or str(v).strip() == '':
                    continue
                raw_count += 1
                text = compact_value(v, max_len=520)
                raw_chips.append(f"<div class='field raw'><span>{esc(field_labels.get(k, k))}</span><strong>{esc(text)}</strong></div>")
                if raw_count >= 120:
                    break
            if raw_chips:
                if raw_count < sum(1 for _k, _v in row.items() if _v is not None and str(_v).strip() != ''):
                    raw_chips.append("<div class='field raw'><span>更多字段</span><strong>超过 120 个原始字段未展开</strong></div>")
                raw_html = (
                    "<details class='raw-fields'><summary>查看完整 DB2 原始字段（含 0 / 默认值 / 内部字段）</summary>"
                    "<div class='fields raw-grid'>" + ''.join(raw_chips) + "</div></details>"
                )
            else:
                raw_html = ''
            return semantic_html + field_html + raw_html

        def table_category(t):
            key = tkey(t)
            for needle, label in category_rules:
                if needle in key:
                    return label
            return '其他 DB2'

        def sort_key_table(item):
            t, c = item
            key = tkey(t)
            cat = table_category(t)
            # 全量 hotfix 报告按所有表排序：先按类别稳定聚合，再按变更量和表名；不再把技能/天赋硬塞到最前。
            return (cat, -int(c or 0), key)

        ordered_stats = sorted(table_stats, key=sort_key_table)
        ordered_keys = [tkey(t) for t, _ in ordered_stats]
        table_lookup = {tkey(t): (t, c) for t, c in table_stats}
        category_counts = {}
        for t, c in table_stats:
            cat = table_category(t)
            bucket = category_counts.setdefault(cat, {'tables': 0, 'rows': 0})
            bucket['tables'] += 1
            bucket['rows'] += int(c or 0)

        category_cards = []
        for cat, meta in sorted(category_counts.items(), key=lambda x: (-x[1]['rows'], x[0])):
            category_cards.append(
                f"<div class='metric category'><span>{esc(cat)}</span><strong>{int(meta['rows'])}</strong><em>{int(meta['tables'])} 张表</em></div>"
            )

        stats_rows = []
        for t, c in ordered_stats:
            cat = table_category(t)
            stats_rows.append(
                f"<tr><td class='tbl'><a href='#table-{esc(tkey(t))}'>{esc(table_label(t))}</a><small>{esc(cat)}</small></td><td class='num'>{int(c or 0)}</td></tr>"
            )

        toc_items = []
        current_cat = None
        for key in ordered_keys[:120]:
            t, c = table_lookup.get(key) or (key, 0)
            cat = table_category(t)
            if cat != current_cat:
                toc_items.append(f"<div class='toc-cat'>{esc(cat)}</div>")
                current_cat = cat
            toc_items.append(f"<a href='#table-{esc(key)}'>{esc(table_label(t))}<span>{int(c or 0)}</span></a>")

        relationship_cards = []
        for key in ordered_keys[:120]:
            t, c = table_lookup.get(key) or (key, 0)
            relation = table_relationship(t)
            relationship_cards.append(
                f"<article class='system-card' data-search='{esc((table_system(t)+' '+table_label(t)+' '+relation).lower())}'>"
                f"<div><span>{esc(table_system(t))}</span><strong>{esc(table_label(t))}</strong></div>"
                f"<p><b>字段关系：</b>{esc(relation)}</p>"
                f"<a href='#table-{esc(key)}'>查看 {int(c or 0)} 条 DB2 记录</a>"
                "</article>"
            )
        system_overview = (
            "<section class='systems-overview' id='systems-overview'>"
            "<div class='table-head'><div><h2>先看对象和字段</h2>"
            "<p>先列 DB2 表之间的关联路径和关键字段含义；下方再展示对象卡片与原始字段。</p></div></div>"
            + ''.join(relationship_cards)
            + "</section>"
        ) if relationship_cards else ''

        detail_sections = []
        hotfix_sample_rows = []
        for table_name, _count in table_stats:
            raw_rows = by_table.get(table_name) or by_table.get(norm_table(table_name)) or []
            seen_rids = set()
            for raw_row in raw_rows:
                rid = self._to_int((raw_row or {}).get('record_id') or 0)
                if rid <= 0 or rid in seen_rids:
                    continue
                seen_rids.add(rid)
                hotfix_sample_rows.append(raw_row)
                if len(seen_rids) >= sample_per_table:
                    break

        object_graph = None
        try:
            object_graph = WagoDB2GraphService(
                build=db2_build,
                locale=locale,
                client=WagoDB2Client(build=db2_build, locale=locale, provider=self),
                max_enrich=max(0, int(enrich_max or 0)),
            ).resolve_hotfix_rows(hotfix_sample_rows)
        except Exception:
            object_graph = None

        def object_fields_html(fields):
            chips = []
            for item in fields or []:
                label = (item or {}).get('label')
                value = (item or {}).get('value')
                if value is None or str(value) == '':
                    continue
                chips.append(f"<div class='field primary'><span>{esc(label)}</span><strong>{esc(value)}</strong></div>")
            return "<div class='fields'>" + ''.join(chips) + "</div>" if chips else ""

        object_cards = []
        for obj in list(getattr(object_graph, 'objects', []) or [])[:120]:
            title = f"{obj.category or obj.kind} · {obj.title or obj.object_id}"
            source_bits = []
            for ref in (obj.source_records or [])[:8]:
                source_bits.append(f"<code>{esc(ref.table)} #{int(ref.record_id or 0)}</code>")
            tags = ''.join(f"<span>{esc(tag)}</span>" for tag in (obj.tags or [])[:8])
            search_text = ' '.join([
                obj.kind or '', str(obj.object_id or ''), obj.title or '', obj.category or '',
                ' '.join(obj.tags or []), ' '.join(f"{r.table} {r.record_id}" for r in obj.source_records or []),
                ' '.join(f"{(f or {}).get('label')} {(f or {}).get('value')}" for f in obj.summary_fields or []),
            ]).lower()
            object_cards.append(
                f"<article class='object-card record' data-search='{esc(search_text)}'>"
                f"<div class='record-head'><div><span class='object-kind'>{esc(obj.kind)}</span><strong>{esc(title)}</strong><div class='object-tags'>{tags}</div></div><div class='object-sources'>{''.join(source_bits)}</div></div>"
                + object_fields_html(obj.summary_fields)
                + "</article>"
            )
        unresolved_count = len(getattr(object_graph, 'unresolved_records', []) or []) if object_graph else 0
        object_graph_section = ""
        if object_cards:
            object_graph_section = (
                "<section class='object-section table-section' id='object-graph' data-search='具体游戏对象 字段关系'>"
                "<div class='table-head'><div><h2>具体游戏对象</h2>"
                f"<p>按 DB2 关系还原到技能、任务、物品、坐骑、宠物、载具等对象；已还原 {len(object_cards)} 个对象，未识别 {unresolved_count} 条样例。</p></div>"
                "<a href='#table-list'>继续看 DB2 表明细</a></div>"
                + ''.join(object_cards)
                + "</section>"
            )

        for key in ordered_keys:
            t, c = table_lookup.get(key) or (key, 0)
            raw_rows = by_table.get(t) or by_table.get(norm_table(t)) or []
            records = []
            seen = set()
            for r in raw_rows:
                rid = self._to_int((r or {}).get('record_id') or 0)
                if rid <= 0 or rid in seen:
                    continue
                seen.add(rid)
                pid = self._to_int((r or {}).get('push_id') or to_push)
                row = fetch_row(t, rid)
                summary = summarize_row(t, rid, row)
                records.append({
                    'record_id': rid,
                    'push_id': pid,
                    'summary': summary,
                    'row': row,
                })
                if len(records) >= sample_per_table:
                    break
            if not records:
                continue
            cards = []
            for rec in records:
                rid = rec['record_id']
                pid = rec['push_id']
                summary = rec.get('summary') or ''
                row = rec.get('row')
                search_text = ' '.join([norm_table(t), table_label(t), str(rid), str(pid), summary, first_text(row)]).lower()
                row_title = summary or f"record_id {rid}"
                wago_rec_url = hotfix_table_url(t, pid)
                cards.append(
                    "<article class='record' data-search='{}'>".format(esc(search_text))
                    + f"<div class='record-head'><div><span class='record-id'>#{rid}</span><strong>{esc(row_title)}</strong></div><a href='{esc(wago_rec_url)}' target='_blank' rel='noreferrer'>Wago push {pid}</a></div>"
                    + row_fields_html(t, rid, row)
                    + "</article>"
                )
            more = max(0, int(c or 0) - len(records))
            more_html = f"<div class='muted more'>还有 {more} 条未展开；可用上方 Wago 表链接查看完整列表。</div>" if more else ''
            section_class = 'table-section'
            detail_sections.append(
                f"<section class='{section_class}' id='table-{esc(key)}' data-search='{esc((norm_table(t)+' '+table_label(t)+' '+table_category(t)).lower())}'>"
                f"<div class='table-head'><div><h2>{esc(table_label(t))}</h2><p>{esc(table_category(t))} · {int(c or 0)} 项 hotfix 记录，展开 {len(records)} 项可读样例</p></div><a href='{esc(hotfix_table_url(t, to_push))}' target='_blank' rel='noreferrer'>Wago 表筛选</a></div>"
                + ''.join(cards)
                + more_html
                + "</section>"
            )

        html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{esc(summary_title)}</title>
  <style>
    body {{ margin:0; padding:16px; font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,'Noto Sans',sans-serif; color:#0f172a; background:#eef2f7; line-height:1.55; }}
    a {{ color:#2563eb; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
    .card {{ max-width:1360px; margin:0 auto; background:#fff; border:1px solid rgba(148,163,184,.35); border-radius:18px; padding:20px; box-shadow:0 18px 48px rgba(15,23,42,.08); }}
    .hero {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; border-bottom:1px solid #e2e8f0; padding-bottom:14px; margin-bottom:14px; }}
    h1 {{ margin:0; font-size:24px; line-height:1.25; letter-spacing:-.02em; }}
    .meta {{ color:#475569; font-size:12px; margin-top:7px; display:flex; flex-wrap:wrap; gap:8px 12px; }}
    .summary {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:14px 0; }}
    .metric {{ border:1px solid #e2e8f0; background:linear-gradient(180deg,#f8fafc,#fff); border-radius:14px; padding:11px 12px; }}
    .metric span {{ display:block; color:#64748b; font-size:12px; }} .metric strong {{ display:block; font-size:20px; margin-top:2px; }} .metric em {{ display:block; color:#64748b; font-style:normal; font-size:11px; margin-top:2px; }}
    .categories {{ grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); }} .metric.category {{ background:#fff; }}
    .note {{ border:1px solid #bfdbfe; background:#eff6ff; color:#1e3a8a; border-radius:14px; padding:10px 12px; font-size:13px; margin:12px 0; }}
    .controls {{ position:sticky; top:0; z-index:5; margin:12px 0; padding:10px; background:rgba(255,255,255,.94); backdrop-filter:blur(8px); border:1px solid #e2e8f0; border-radius:14px; display:flex; gap:10px; align-items:center; }}
    .controls input {{ width:100%; border:1px solid #cbd5e1; border-radius:10px; padding:9px 11px; font-size:14px; }} .controls .count {{ white-space:nowrap; color:#64748b; font-size:12px; }}
    .toc {{ margin:12px 0; padding:12px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:14px; display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:8px; }}
    .toc-title {{ grid-column:1/-1; font-weight:900; }} .toc-cat {{ grid-column:1/-1; margin-top:6px; color:#475569; font-size:12px; font-weight:900; }} .toc a {{ display:flex; justify-content:space-between; gap:8px; color:#0f172a; background:#fff; border:1px solid #e2e8f0; border-radius:11px; padding:7px 9px; }} .toc span {{ color:#64748b; font-size:12px; }}
    .layout {{ display:grid; grid-template-columns:minmax(260px,360px) 1fr; gap:14px; align-items:start; }}
    .stats {{ position:sticky; top:66px; max-height:calc(100vh - 88px); overflow:auto; border:1px solid #e2e8f0; border-radius:14px; background:#fff; }}
    table {{ border-collapse:collapse; width:100%; font-size:13px; }} th,td {{ border-bottom:1px solid #e2e8f0; padding:8px 10px; vertical-align:top; }} th {{ background:#f8fafc; text-align:left; position:sticky; top:0; }} td.num {{ text-align:right; width:72px; font-weight:900; }} td.tbl {{ overflow-wrap:anywhere; }} td.tbl small {{ display:block; color:#64748b; font-size:11px; margin-top:2px; }}
    .table-section {{ margin:0 0 14px; padding:14px; border:1px solid #dbeafe; border-radius:16px; background:#f8fbff; scroll-margin-top:72px; }}
    .table-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:10px; }} .table-head h2 {{ margin:0; font-size:20px; }} .table-head p {{ margin:4px 0 0; color:#64748b; font-size:12px; }}
    .record {{ margin-top:10px; padding:11px 13px; background:#fff; border:1px solid #e2e8f0; border-radius:12px; box-shadow:0 1px 2px rgba(15,23,42,.04); }}
    .record-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; font-size:13px; }} .record-head strong {{ display:block; margin-top:2px; overflow-wrap:anywhere; }} .record-id {{ display:inline-flex; color:#2563eb; font-weight:900; margin-right:6px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
    .object-section {{ border-color:#c7d2fe; background:#f8f7ff; }} .object-card {{ border-color:#ddd6fe; }} .object-kind {{ display:inline-flex; color:#7c3aed; font-weight:900; margin-right:6px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }} .object-tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:5px; }} .object-tags span {{ color:#4c1d95; background:#ede9fe; border:1px solid #ddd6fe; border-radius:999px; padding:1px 7px; font-size:11px; font-weight:800; }} .object-sources {{ display:flex; flex-wrap:wrap; gap:5px; justify-content:flex-end; }}
    .systems-overview {{ margin:0 0 14px; padding:14px; border:1px solid #bbf7d0; border-radius:16px; background:#f0fdf4; }} .systems-overview h2 {{ margin:0; font-size:20px; }} .system-card {{ margin-top:10px; padding:12px 13px; background:#fff; border:1px solid #dcfce7; border-radius:13px; }} .system-card div {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }} .system-card span {{ color:#166534; background:#dcfce7; border:1px solid #bbf7d0; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:900; }} .system-card strong {{ font-size:15px; }} .system-card p {{ margin:7px 0 3px; color:#0f172a; }} .system-card small {{ display:block; color:#64748b; margin-bottom:6px; }}
    .semantic-fallback {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; margin-top:9px; }} .semantic-line {{ border:1px solid #bbf7d0; background:#f0fdf4; border-radius:10px; padding:7px 9px; }} .semantic-line span {{ display:block; color:#166534; font-size:11px; font-weight:900; }} .semantic-line strong {{ display:block; color:#0f172a; font-size:13px; overflow-wrap:anywhere; white-space:pre-wrap; }}
    .fields {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:8px; margin-top:9px; }} .important-fields {{ grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }} .field {{ border:1px solid #e2e8f0; background:#f8fafc; border-radius:10px; padding:7px 9px; min-width:0; }} .field.primary {{ border-color:#bfdbfe; background:#eff6ff; }} .field.important {{ border-color:#fde68a; background:#fffbeb; }} .field span {{ display:block; color:#64748b; font-size:11px; font-weight:800; }} .field strong {{ display:block; color:#0f172a; font-size:13px; overflow-wrap:anywhere; white-space:pre-wrap; }} .raw-fields {{ margin-top:9px; border:1px dashed #cbd5e1; border-radius:10px; background:#f8fafc; padding:7px 9px; }} .raw-fields summary {{ cursor:pointer; color:#64748b; font-size:12px; font-weight:900; }} .raw-grid {{ grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }} .raw-grid .field {{ background:#fff; opacity:.82; }}
    code {{ background:#f1f5f9; padding:1px 6px; border-radius:4px; }} .muted {{ color:#64748b; font-size:13px; }} .more {{ margin-top:10px; }} .hidden {{ display:none!important; }}
    @media(max-width:920px) {{ body{{padding:8px}} .card{{padding:14px}} .hero{{display:block}} .layout{{display:block}} .stats{{position:static; max-height:none; margin-bottom:14px}} .controls{{top:0}} }}
  </style>
</head>
<body>
  <main class="card">
    <div class="hero">
      <div>
        <h1>{esc(summary_title)}</h1>
        <div class="meta"><span>分支：{esc(branch)}</span><span>区域/语言：{esc(locale)}</span><span>Build：{esc(build_num)}</span><span>Push：{int(from_push or 0)} → {int(to_push or 0)}</span></div>
      </div>
      <div class="meta"><a href="{esc(wago_url)}" target="_blank" rel="noreferrer">Wago 原始 Hotfix 列表</a></div>
    </div>
    <div class="summary">
      <div class="metric"><span>Hotfix 记录</span><strong>{entry_count}</strong></div>
      <div class="metric"><span>DB2 表</span><strong>{table_count}</strong></div>
      <div class="metric"><span>已还原类别</span><strong>{len(category_counts)}</strong></div>
      <div class="metric"><span>每表展开</span><strong>{sample_per_table}</strong></div>
    </div>
    <div class="summary categories">{''.join(category_cards)}</div>
    <div class="note">Hotfix 原始数据只给出 push / DB2 表 / record_id。本报告按所有涉及的 DB2 表逐表读取当前 build 的记录，尽可能还原名称、描述、数值、ID、关联字段和原始字段快照；技能/天赋只是其中一类，不会过滤掉任务、物品、地图、生物、货币、成就等其他改动。完整原始列表仍可通过每个表的 Wago 链接复核。</div>
    <div class="controls"><input id="hotfixFilter" type="search" placeholder="筛选类别、表名、record_id、名称、描述、字段值…" autocomplete="off"><span class="count" id="filterCount">全部显示</span></div>
    <nav class="toc" aria-label="DB2 表目录"><div class="toc-title">DB2 表目录（按类别分组，覆盖全部表）</div>{''.join(toc_items)}</nav>
    <div class="layout">
      <aside class="stats" id="table-list">
        <table><thead><tr><th>DB2 表</th><th class="num">数量</th></tr></thead><tbody>{''.join(stats_rows)}</tbody></table>
      </aside>
      <section class="details">{system_overview}{object_graph_section}{''.join(detail_sections)}</section>
    </div>
  </main>
  <script>
(function(){{
  var input=document.getElementById('hotfixFilter');
  var count=document.getElementById('filterCount');
  if(!input){{return;}}
  function apply(){{
    var q=(input.value||'').trim().toLowerCase();
    var total=0, visible=0;
    document.querySelectorAll('.record').forEach(function(el){{
      total++;
      var ok=!q || (el.getAttribute('data-search')||'').indexOf(q)>=0 || (el.textContent||'').toLowerCase().indexOf(q)>=0;
      el.classList.toggle('hidden', !ok);
      if(ok){{visible++;}}
    }});
    document.querySelectorAll('.table-section').forEach(function(sec){{
      var ok=!q || !!sec.querySelector('.record:not(.hidden)') || (sec.getAttribute('data-search')||'').indexOf(q)>=0;
      sec.classList.toggle('hidden', !ok);
    }});
    if(count){{count.textContent=q ? ('显示 '+visible+' / '+total+' 条记录') : ('全部 '+total+' 条展开记录');}}
  }}
  input.addEventListener('input', apply);
  apply();
}})();
  </script>
</body>
</html>
"""
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(html_text)
        except Exception:
            return "", ""
        return full_path, rel_path

    def _generate_hotfix_report(self, branch, current_build, from_push, to_push, *, locale=''):
        max_pages = int(getattr(settings, 'WAGO_HOTFIX_MAX_PAGES', 8) or 8)
        max_pushes = int(getattr(settings, 'WAGO_HOTFIX_MAX_PUSHES', 20) or 20)
        max_entries = int(getattr(settings, 'WAGO_HOTFIX_MAX_ENTRIES', 2000) or 2000)

        hotfix_rows = []
        push_ids = set()
        locale = (locale or '').strip() or self.locale
        search = locale
        page = 1
        while page <= max_pages and len(hotfix_rows) < max_entries:
            raw = self._fetch_hotfix_page_data(page=page, search=search) or []
            if not raw:
                break
            min_pid = 0
            for r in raw:
                pid = self._to_int((r or {}).get('push_id') or 0)
                if pid <= 0:
                    continue
                if min_pid <= 0 or pid < min_pid:
                    min_pid = pid
                if locale and (str((r or {}).get('locale') or '').strip() or '') != locale:
                    continue
                if pid <= int(from_push or 0):
                    continue
                if int(to_push or 0) > 0 and pid > int(to_push or 0):
                    continue
                hotfix_rows.append(r)
                push_ids.add(pid)
                if len(hotfix_rows) >= max_entries:
                    break
            if min_pid > 0 and min_pid <= int(from_push or 0):
                break
            page += 1

        if not hotfix_rows:
            return None

        push_sorted = sorted(push_ids)
        if max_pushes > 0 and len(push_sorted) > max_pushes:
            keep = set(push_sorted[-max_pushes:])
            hotfix_rows = [r for r in hotfix_rows if self._to_int((r or {}).get('push_id') or 0) in keep]
            push_sorted = sorted(keep)

        class_names = self._load_chr_classes(current_build)
        spec_meta = self._load_chr_specialization_meta(current_build)
        spec_to_class = {sid: meta.get('class_id') for sid, meta in (spec_meta or {}).items() if meta.get('class_id')}
        spell_to_specs = self._load_specialization_spells(current_build)
        if not spec_to_class or not spell_to_specs:
            return None

        spell_ids = set()
        effect_records = []
        for r in hotfix_rows:
            tkey = (str((r or {}).get('table_name') or '').strip() or '').lower()
            rid = self._to_int((r or {}).get('record_id') or 0)
            if not tkey or rid <= 0:
                continue
            if tkey not in self.core_tables:
                continue
            if tkey == 'spelleffect':
                row = self._fetch_db2_row_by_id('SpellEffect', current_build, rid)
                sid = self._extract_spell_id('spelleffect', row)
                if sid <= 0:
                    continue
                try:
                    eff_idx = int(str((row or {}).get('EffectIndex') or '0').strip() or '0')
                except Exception:
                    eff_idx = 0
                effect_records.append({'id': rid, 'spell_id': sid, 'effect_index': eff_idx})
                spell_ids.add(sid)
            else:
                spell_ids.add(rid)

        spell_ids = sorted(set(int(x) for x in spell_ids if int(x) > 0))
        if not spell_ids:
            return None

        filtered_spell_ids = []
        for sid in spell_ids:
            if self._spell_has_class(sid, spell_to_specs, spec_to_class, current_build):
                filtered_spell_ids.append(sid)
        spell_ids = filtered_spell_ids
        if not spell_ids:
            return None

        snap_spells = {}
        for sid in spell_ids:
            snap_spells[sid] = {}

        existing_spell_rows = {
            int(r.spell_id): r
            for r in WowSpellSnapshot.objects.filter(branch=branch, locale=locale, spell_id__in=spell_ids)
        }
        existing_eff_rows = {
            (int(r.spell_id), int(r.effect_index)): r
            for r in WowSpellEffectSnapshot.objects.filter(branch=branch, locale=locale, spell_id__in=spell_ids)
        }

        need_name = []
        for sid in spell_ids:
            n = (existing_spell_rows.get(sid).name if existing_spell_rows.get(sid) else '').strip()
            if not n:
                need_name.append(sid)
        if need_name:
            fetched = self._fetch_spell_names_concurrent(current_build, need_name)
            for sid, nm in (fetched or {}).items():
                if nm:
                    snap_spells.setdefault(int(sid), {})
                    snap_spells[int(sid)]['name'] = nm

        for sid in spell_ids:
            row = self._fetch_db2_row_by_id('SpellName', current_build, sid)
            n = (row.get('Name_lang') if isinstance(row, dict) else None)
            if n is not None:
                snap_spells.setdefault(sid, {})
                snap_spells[sid]['name'] = str(n)
            row = self._fetch_db2_row_by_id('SpellDescription', current_build, sid)
            if isinstance(row, dict):
                d = row.get('Description_lang')
                a = row.get('AuraDescription_lang')
                if d is not None:
                    snap_spells.setdefault(sid, {})
                    snap_spells[sid]['description'] = str(d)
                if a is not None:
                    snap_spells.setdefault(sid, {})
                    snap_spells[sid]['aura_description'] = str(a)

        snap_effects = {}
        for rec in effect_records:
            sid = int(rec.get('spell_id') or 0)
            if sid <= 0 or sid not in set(spell_ids):
                continue
            rid = int(rec.get('id') or 0)
            if rid <= 0:
                continue
            row = self._fetch_db2_row_by_id('SpellEffect', current_build, rid)
            if not isinstance(row, dict):
                continue
            try:
                eff_idx = int(str((row or {}).get('EffectIndex') or '0').strip() or '0')
            except Exception:
                eff_idx = int(rec.get('effect_index') or 0)
            key = (sid, int(eff_idx or 0))
            e = {}
            for k, nk in (('Effect', 'effect'), ('EffectAura', 'effect_aura')):
                try:
                    e[nk] = int(str((row or {}).get(k) or '').strip() or '0')
                except Exception:
                    e[nk] = None
            bp = (row or {}).get('EffectBasePointsF')
            if bp is None or bp == '':
                bp = (row or {}).get('EffectBasePoints')
            coef = (row or {}).get('EffectBonusCoefficient')
            if coef is None or coef == '':
                coef = (row or {}).get('BonusCoefficientFromAP')
            if coef is None or coef == '':
                coef = (row or {}).get('Coefficient')
            pvp = (row or {}).get('PvpMultiplier')
            e['base_points'] = str(bp if bp is not None else '')
            e['coefficient'] = str(coef if coef is not None else '')
            e['pvp_multiplier'] = str(pvp if pvp is not None else '')
            snap_effects[key] = e

        spell_changes = {}
        changed_tables = set()
        for sid in spell_ids:
            before = existing_spell_rows.get(sid)
            after = snap_spells.get(sid) or {}
            diffs_by_table = {}
            if 'name' in after:
                b = (before.name if before else '') or ''
                a = after.get('name') or ''
                if str(b) != str(a):
                    action = 'added' if not b and a else ('removed' if b and not a else 'changed')
                    diffs_by_table.setdefault('spellname', []).append({
                        'id': sid,
                        'action': action,
                        'fields': self._filter_diff_fields('spellname', [{'field': 'Name_lang', 'before': str(b), 'after': str(a)}]),
                    })
                    changed_tables.add('spellname')
            for fk, label in (('description', 'Description_lang'), ('aura_description', 'AuraDescription_lang')):
                if fk in after:
                    b = (getattr(before, fk) if before else '') or ''
                    a = after.get(fk) or ''
                    if str(b) != str(a):
                        action = 'added' if not b and a else ('removed' if b and not a else 'changed')
                        diffs_by_table.setdefault('spelldescription', []).append({
                            'id': sid,
                            'action': action,
                            'fields': self._filter_diff_fields('spelldescription', [{'field': label, 'before': str(b), 'after': str(a)}]),
                        })
                        changed_tables.add('spelldescription')

            for (esid, eff_idx), patch in snap_effects.items():
                if esid != sid:
                    continue
                before_eff = existing_eff_rows.get((sid, eff_idx))
                fields = []
                b_effect = str(before_eff.effect) if before_eff and before_eff.effect is not None else ''
                a_effect = str(patch.get('effect') if patch.get('effect') is not None else '')
                fields.append({'field': 'Effect', 'before': b_effect, 'after': a_effect})
                b_aura = str(before_eff.effect_aura) if before_eff and before_eff.effect_aura is not None else ''
                a_aura = str(patch.get('effect_aura') if patch.get('effect_aura') is not None else '')
                fields.append({'field': 'EffectAura', 'before': b_aura, 'after': a_aura})
                b_bp = (before_eff.base_points if before_eff else '') or ''
                a_bp = (patch.get('base_points') or '')
                fields.append({'field': 'EffectBasePointsF', 'before': str(b_bp), 'after': str(a_bp)})
                b_coef = (before_eff.coefficient if before_eff else '') or ''
                a_coef = (patch.get('coefficient') or '')
                fields.append({'field': 'EffectBonusCoefficient', 'before': str(b_coef), 'after': str(a_coef)})
                b_pvp = (before_eff.pvp_multiplier if before_eff else '') or ''
                a_pvp = (patch.get('pvp_multiplier') or '')
                fields.append({'field': 'PvpMultiplier', 'before': str(b_pvp), 'after': str(a_pvp)})
                filtered_fields = self._filter_diff_fields('spelleffect', fields)
                if filtered_fields:
                    action = 'added' if not before_eff else 'changed'
                    diffs_by_table.setdefault('spelleffect', []).append({
                        'id': eff_idx,
                        'action': action,
                        'meta': {'EffectIndex': eff_idx},
                        'fields': filtered_fields,
                    })
                    changed_tables.add('spelleffect')

            visible = False
            for tkey, items in diffs_by_table.items():
                for it in items or []:
                    if it.get('fields'):
                        visible = True
                        break
                if visible:
                    break
            if visible:
                spell_changes[sid] = {'tables': set(diffs_by_table.keys()), 'diffs': diffs_by_table}

        if not spell_changes:
            now = timezone.now()
            spell_objs = []
            for sid, patch in snap_spells.items():
                spell_objs.append(
                    WowSpellSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spell_id=sid,
                        name=(patch.get('name') or '')[:255],
                        description=patch.get('description') or '',
                        aura_description=patch.get('aura_description') or '',
                        snapshot_build=current_build,
                        updated_at=now,
                    )
                )
            if spell_objs:
                self._bulk_upsert_snapshots(
                    WowSpellSnapshot,
                    spell_objs,
                    unique_fields=['branch', 'locale', 'spell_id'],
                    update_fields=['name', 'description', 'aura_description', 'snapshot_build', 'updated_at'],
                )
            if snap_effects:
                eff_objs = []
                for (sid, eff_idx), patch in snap_effects.items():
                    eff_objs.append(
                        WowSpellEffectSnapshot(
                            branch=branch,
                            locale=self.locale,
                            spell_id=sid,
                            effect_index=eff_idx,
                            effect=patch.get('effect'),
                            effect_aura=patch.get('effect_aura'),
                            base_points=patch.get('base_points') or '',
                            coefficient=patch.get('coefficient') or '',
                            pvp_multiplier=patch.get('pvp_multiplier') or '',
                            snapshot_build=current_build,
                            updated_at=now,
                        )
                    )
                self._bulk_upsert_snapshots(
                    WowSpellEffectSnapshot,
                    eff_objs,
                    unique_fields=['branch', 'locale', 'spell_id', 'effect_index'],
                    update_fields=['effect', 'effect_aura', 'base_points', 'coefficient', 'pvp_multiplier', 'snapshot_build', 'updated_at'],
                )
            WowSpellSnapshotState.objects.update_or_create(
                branch=branch,
                locale=self.locale,
                defaults={'snapshot_build': current_build},
            )
            return None

        snap_map_add = set()
        for sid in spell_changes.keys():
            specs = spell_to_specs.get(sid) or set()
            for spec_id in specs:
                snap_map_add.add((spec_id, sid))
            snap_spells.setdefault(sid, {})

        now = timezone.now()
        if snap_map_add:
            map_objs = []
            for spec_id, spell_id in snap_map_add:
                map_objs.append(
                    WowSpecSpellMapSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spec_id=spec_id,
                        spell_id=spell_id,
                        snapshot_build=current_build,
                        updated_at=now,
                    )
                )
            self._bulk_upsert_snapshots(
                WowSpecSpellMapSnapshot,
                map_objs,
                unique_fields=['branch', 'locale', 'spec_id', 'spell_id'],
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
                        snapshot_build=current_build,
                        updated_at=now,
                    )
                )
            self._bulk_upsert_snapshots(
                WowSpellSnapshot,
                spell_objs,
                unique_fields=['branch', 'locale', 'spell_id'],
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
                        snapshot_build=current_build,
                        updated_at=now,
                    )
                )
            self._bulk_upsert_snapshots(
                WowSpellEffectSnapshot,
                eff_objs,
                unique_fields=['branch', 'locale', 'spell_id', 'effect_index'],
                update_fields=['effect', 'effect_aura', 'base_points', 'coefficient', 'pvp_multiplier', 'snapshot_build', 'updated_at'],
            )

        WowSpellSnapshotState.objects.update_or_create(
            branch=branch,
            locale=self.locale,
            defaults={'snapshot_build': current_build},
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
            zh_name_lookup = self._ensure_spell_names_zh(branch, current_build, list(spell_changes.keys()))
            samples = self._extract_summary_samples(spell_changes, snap_spells, max_samples=10, name_lookup=zh_name_lookup)
            summary_title = self._glm_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples) or self._heuristic_summary_title(class_spell_counts, len(spell_changes), changed_tables, samples=samples)

        display_from = f"{current_build} Hotfix#{int(from_push)}"
        display_to = f"{current_build} Hotfix#{int(to_push)}"
        from_build_key = f"{current_build}-hf{int(from_push)}"
        to_build_key = f"{current_build}-hf{int(to_push)}"

        content_md = f"# {summary_title or (server_title + ' 职业技能变更报告')}\n\n- 版本：{display_from} → {display_to}\n- 技能数：{len(spell_changes)}\n"
        html_meta = self._write_html_report(
            branch=branch,
            server_title=server_title,
            from_build=from_build_key,
            to_build=to_build_key,
            display_from_build=display_from,
            display_to_build=display_to,
            class_names=class_names,
            spec_meta=spec_meta,
            spell_to_specs=spell_to_specs,
            spec_to_class=spec_to_class,
            spell_changes=spell_changes,
            data_build=current_build,
        )
        content_html_path = (html_meta or {}).get('path') or ''
        class_count = int((html_meta or {}).get('class_count') or 0)
        return {
            'summary_title': summary_title or '',
            'from_build': from_build_key,
            'to_build': to_build_key,
            'display_from_build': display_from,
            'display_to_build': display_to,
            'content_md': content_md,
            'content_html_path': content_html_path or '',
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
        next_url = url
        visited = set()
        tables = set()
        while next_url and next_url not in visited:
            visited.add(next_url)
            text = self._http_get_text(next_url)
            if not text:
                raise WagoDiffUnavailable(
                    f'Wago builds-diff is not available for {from_build} -> {to_build}: {next_url}'
                )
            props = self._extract_inertia_props(text)
            payload = props.get('items') or {}
            items = payload.get('data') if isinstance(payload, dict) else payload
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
            next_url = payload.get('next_page_url') if isinstance(payload, dict) else None
            if next_url and next_url.startswith('/'):
                next_url = "https://wago.tools" + next_url
            if next_url and 'from=' not in next_url and 'to=' not in next_url:
                sep = '&' if '?' in next_url else '?'
                next_url = f"{next_url}{sep}from={from_build}&to={to_build}"
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
                raise WagoDiffUnavailable(
                    f'Wago DB2 diff is not available for {table} {from_build} -> {to_build}: {next_url}'
                )
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

    def _load_chr_classes(self, build, locale_override=None):
        build = (build or '').strip()
        use_locale = (locale_override or self.locale or '').strip() or 'enUS'
        key = (build, use_locale)
        if key in self._chr_classes_cache:
            return self._chr_classes_cache.get(key) or {}
        url = f"https://wago.tools/db2/ChrClasses/csv?build={build}&locale={use_locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._chr_classes_cache[key] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._chr_classes_cache[key] = {}
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
        self._chr_classes_cache[key] = out
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

    def _load_chr_specialization_meta(self, build, locale_override=None):
        build = (build or '').strip()
        use_locale = (locale_override or self.locale or '').strip() or 'enUS'
        key = (build, use_locale)
        if key in self._chr_specialization_meta_cache:
            return self._chr_specialization_meta_cache.get(key) or {}
        url = f"https://wago.tools/db2/ChrSpecialization/csv?build={build}&locale={use_locale}"
        content = self._http_get_bytes(url, timeout=max(60, self.http_timeout))
        if not content:
            self._chr_specialization_meta_cache[key] = {}
            return {}
        try:
            text = content.decode('utf-8', 'replace')
        except Exception:
            self._chr_specialization_meta_cache[key] = {}
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
        self._chr_specialization_meta_cache[key] = out
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

    def _http_get(self, url, timeout=None, *, binary=False, attempts=None):
        timeout = max(int(timeout or self.http_timeout or 30), int(getattr(settings, 'WAGO_SKILL_DIFF_MIN_TIMEOUT', 45) or 45))
        attempts = int(attempts or getattr(settings, 'WAGO_SKILL_DIFF_HTTP_RETRIES', 3) or 3)
        attempts = max(1, attempts)
        last_error = ''
        for attempt in range(1, attempts + 1):
            started = time.time()
            logger.info(f"[WagoSkillDiffMonitor] request {attempt}/{attempts} {url}")
            try:
                resp = self._http_session.get(url, timeout=timeout)
                elapsed = time.time() - started
                if resp.status_code == 200:
                    size = len(resp.content or b'')
                    logger.info(f"[WagoSkillDiffMonitor] request done status=200 size={size} elapsed={elapsed:.1f}s {url}")
                    return resp.content if binary else (resp.text or '')
                last_error = f"status={resp.status_code} elapsed={elapsed:.1f}s"
                logger.warning(f"[WagoSkillDiffMonitor] request failed {attempt}/{attempts} {url}: {last_error}")
            except Exception as e:
                elapsed = time.time() - started
                last_error = f"{e} elapsed={elapsed:.1f}s"
                logger.warning(f"[WagoSkillDiffMonitor] request error {attempt}/{attempts} {url}: {last_error}")
            if attempt < attempts:
                time.sleep(min(2 * attempt, 8))
        logger.warning(f"[WagoSkillDiffMonitor] request exhausted {url}: {last_error}")
        return b'' if binary else ''

    def _http_get_text(self, url, timeout=None):
        return self._http_get(url, timeout=timeout, binary=False)

    def _http_get_bytes(self, url, timeout=None):
        return self._http_get(url, timeout=timeout, binary=True)

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

    def _fetch_db2_row_by_id_requests(self, table, build, record_id, locale_override=None):
        table = (table or '').strip()
        build = (build or '').strip()
        try:
            record_id = int(record_id)
        except Exception:
            return {}
        if not table or not build or record_id <= 0:
            return {}
        use_locale = (locale_override or self.locale or '').strip() or 'enUS'
        url = f"https://wago.tools/db2/{table}?build={build}&locale={use_locale}&filter[ID]=exact:{record_id}"
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

    def _fetch_db2_rows_by_ids_requests(self, table, build, record_ids, locale_override=None, chunk_size=80):
        """批量拉取 DB2 行，显著减少 HTTP 次数。

        wago.tools 支持 filter[ID]=in:1,2,3 这种写法。
        """
        table = (table or '').strip()
        build = (build or '').strip()
        if not table or not build:
            return {}
        use_locale = (locale_override or self.locale or '').strip() or 'enUS'
        ids = []
        for rid in record_ids or []:
            try:
                rid_int = int(rid)
            except Exception:
                continue
            if rid_int > 0:
                ids.append(rid_int)
        if not ids:
            return {}

        chunk_size = max(10, min(int(chunk_size or 80), 200))
        out = {}
        for start in range(0, len(ids), chunk_size):
            batch = ids[start:start + chunk_size]
            joined = ','.join(str(x) for x in batch)
            url = f"https://wago.tools/db2/{table}?build={build}&locale={use_locale}&filter[ID]=in:{joined}"
            try:
                r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
            except Exception:
                continue
            if r.status_code != 200:
                continue
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
            for row in data if isinstance(data, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    rid = int(row.get('ID') or 0)
                except Exception:
                    continue
                if rid > 0:
                    out[rid] = row
        return out

    def _fetch_spell_names_concurrent(self, build, spell_ids, locale_override=None):
        spell_ids = [int(x) for x in (spell_ids or []) if int(x) > 0]
        if not spell_ids:
            return {}
        workers = int(getattr(settings, 'WAGO_SPELLNAME_WORKERS', 12) or 12)
        workers = max(1, min(workers, 32))
        out = {}

        def work(spell_id):
            row = self._fetch_db2_row_by_id_requests('SpellName', build, spell_id, locale_override=locale_override)
            name = (row.get('Name_lang') or '').strip()
            return spell_id, name

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for spell_id, name in ex.map(work, spell_ids):
                if name:
                    out[spell_id] = name
        return out

    def _fetch_spell_name_wowhead_cn(self, spell_id):
        try:
            spell_id = int(spell_id)
        except Exception:
            return ''
        if spell_id <= 0:
            return ''
        url = f"https://www.wowhead.com/cn/spell={spell_id}"
        try:
            r = requests.get(url, timeout=max(30, self.http_timeout), headers={'User-Agent': 'Mozilla/5.0'})
        except Exception:
            return ''
        if r.status_code != 200:
            return ''
        t = r.text or ''
        m = re.search(r'<h1[^>]*>\s*([^<]+?)\s*</h1>', t, flags=re.I)
        if m:
            return html.unescape(m.group(1) or '').strip()
        m = re.search(r'<title[^>]*>\s*([^<]+?)\s*</title>', t, flags=re.I)
        if not m:
            return ''
        title = html.unescape(m.group(1) or '').strip()
        title = re.sub(r'\s*-\s*Wowhead\s*$', '', title, flags=re.I).strip()
        title = re.sub(r'\s*-\s*魔兽世界\s*$', '', title, flags=re.I).strip()
        return title

    def _ensure_spell_names_zh(self, branch, build, spell_ids):
        spell_ids = [int(x) for x in (spell_ids or []) if int(x) > 0]
        if not spell_ids:
            return {}
        existing = {
            int(r['spell_id']): (r.get('name_zh') or '').strip()
            for r in WowSpellSnapshot.objects.filter(branch=branch, locale=self.locale, spell_id__in=spell_ids)
            .exclude(name_zh='')
            .values('spell_id', 'name_zh')
        }
        missing = [sid for sid in spell_ids if not (existing.get(sid) or '').strip()]
        fetched = {}
        if missing:
            fetched.update(self._fetch_spell_names_concurrent(build, missing, locale_override=self.name_locale))
        still_missing = [sid for sid in missing if not (fetched.get(sid) or '').strip()]
        if still_missing:
            limit = 50
            for sid in still_missing[:limit]:
                name = (self._fetch_spell_name_wowhead_cn(sid) or '').strip()
                if name:
                    fetched[sid] = name
        if fetched:
            now = timezone.now()
            objs = []
            for sid, name in fetched.items():
                if not (name or '').strip():
                    continue
                objs.append(
                    WowSpellSnapshot(
                        branch=branch,
                        locale=self.locale,
                        spell_id=int(sid),
                        name_zh=(name or '')[:255],
                        snapshot_build=build,
                        updated_at=now,
                    )
                )
            if objs:
                self._bulk_upsert_snapshots(
                    WowSpellSnapshot,
                    objs,
                    unique_fields=['branch', 'locale', 'spell_id'],
                    update_fields=['name_zh', 'snapshot_build', 'updated_at'],
                )
        out = dict(existing)
        for sid, name in fetched.items():
            if (name or '').strip():
                out[int(sid)] = (name or '').strip()
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

    def _write_empty_html_report(self, branch, server_title, from_build, to_build, display_from_build='', display_to_build='', message=''):
        rel_path = f"portal/reports/wow_skill_diff_{branch}_{self.locale}_{to_build.replace('.', '_')}_empty.html"
        base_dir = str(getattr(settings, 'BASE_DIR', '') or '')
        static_dir = os.path.join(base_dir, 'static') if base_dir else os.path.join(os.getcwd(), 'static')
        full_path = os.path.join(static_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        title_from = display_from_build or from_build
        title_to = display_to_build or to_build
        title = f"{server_title} 职业技能变更报告：{title_from} → {title_to}"
        html_text = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
body{{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:0;padding:16px;line-height:1.55;background:#f1f5f9;color:#0f172a}}
.card{{max-width:1200px;margin:0 auto;background:#fff;border:1px solid rgba(148,163,184,.35);border-radius:14px;padding:18px}}
.meta{{color:#475569;font-size:12px;margin-top:6px;display:flex;flex-wrap:wrap;gap:10px}}
.empty{{margin-top:16px;border:1px solid #c7d2fe;background:#eef2ff;color:#3730a3;border-radius:12px;padding:14px}}
</style>
</head>
<body>
<div class="card">
<h1 style="margin:0;font-size:18px">{html.escape(title)}</h1>
<div class="meta"><span>技能数：0</span><span>职业数：0</span><span>语言：{html.escape(self.locale)}</span></div>
<div class="empty">{html.escape(message or '指定版本区间未发现可归属到职业/专精的技能变更。')}</div>
</div>
</body>
</html>
"""
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(html_text)
        except Exception:
            return {}
        return {'path': rel_path, 'class_count': 0}

    def _write_html_report(self, branch, server_title, from_build, to_build, display_from_build, display_to_build, class_names, spec_meta, spell_to_specs, spec_to_class, spell_changes, wowhead_url='', data_build=''):
        data_build = (data_build or '').strip() or to_build
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
            fetched = self._fetch_spell_names_concurrent(data_build, missing)
            name_cache.update(fetched)

        zh_name_cache = self._ensure_spell_names_zh(branch, data_build, spell_ids)
        zh_class_names = self._load_chr_classes(data_build, locale_override=self.name_locale)
        zh_spec_meta = self._load_chr_specialization_meta(data_build, locale_override=self.name_locale)
        display_class_names = dict(class_names or {})
        for cid, nm in (zh_class_names or {}).items():
            if (nm or '').strip():
                display_class_names[int(cid)] = nm
        display_spec_meta = {}
        for sid, meta in (spec_meta or {}).items():
            zh_name = ((zh_spec_meta or {}).get(sid) or {}).get('name') or ''
            m = dict(meta or {})
            if (zh_name or '').strip():
                m['name'] = zh_name
            display_spec_meta[sid] = m

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
                for cid in self._spell_class_ids(spell_id, spell_to_specs, spec_to_class, data_build):
                    class_to_spec_to_spells.setdefault(cid, {}).setdefault(0, set()).add(spell_id)

        class_count = len(class_to_spec_to_spells)
        class_spell_counts = {}
        class_spec_counts = {}
        changed_table_counts = {}
        for cid, spec_map in (class_to_spec_to_spells or {}).items():
            class_spells = set()
            spec_total = 0
            for _spec_id, sids in (spec_map or {}).items():
                spell_set = set(sids or [])
                if spell_set:
                    spec_total += 1
                    class_spells.update(spell_set)
            class_spell_counts[cid] = len(class_spells)
            class_spec_counts[cid] = spec_total
        for entry in (spell_changes or {}).values():
            for tkey in ((entry or {}).get('diffs') or {}).keys():
                changed_table_counts[tkey] = int(changed_table_counts.get(tkey) or 0) + 1

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
        parts.append('body{font-family:ui-sans-serif,system-ui,Segoe UI,Arial;margin:0;padding:16px;line-height:1.55;background:#eef2f7;color:#0f172a}')
        parts.append('.card{max-width:1320px;margin:0 auto;background:#ffffff;border:1px solid rgba(148,163,184,.35);border-radius:18px;padding:20px;box-shadow:0 18px 48px rgba(15,23,42,.08)}')
        parts.append('.hero{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;border-bottom:1px solid #e2e8f0;padding-bottom:14px;margin-bottom:14px}')
        parts.append('.hero h1{margin:0;font-size:22px;line-height:1.25;letter-spacing:-.02em}')
        parts.append('.meta{color:#475569;font-size:12px;margin-top:6px;display:flex;flex-wrap:wrap;gap:8px 12px}')
        parts.append('.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}')
        parts.append('.metric{border:1px solid #e2e8f0;background:linear-gradient(180deg,#f8fafc,#fff);border-radius:14px;padding:11px 12px}')
        parts.append('.metric span{display:block;color:#64748b;font-size:12px}.metric strong{display:block;font-size:18px;margin-top:2px}')
        parts.append('.controls{position:sticky;top:0;z-index:5;margin:12px 0;padding:10px;background:rgba(255,255,255,.94);backdrop-filter:blur(8px);border:1px solid #e2e8f0;border-radius:14px;display:flex;gap:10px;align-items:center}')
        parts.append('.controls input{width:100%;border:1px solid #cbd5e1;border-radius:10px;padding:9px 11px;font-size:14px}.controls .count{white-space:nowrap;color:#64748b;font-size:12px}')
        parts.append('.toc{margin-top:12px;padding:12px;background:#f8fafc;border:1px solid rgba(226,232,240,1);border-radius:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}')
        parts.append('.toc-title{grid-column:1/-1;font-weight:800;margin-bottom:2px}.toc a{color:#1d4ed8;text-decoration:none}.toc a:hover{text-decoration:underline}')
        parts.append('.toc-item{padding:8px 10px;border:1px solid #e2e8f0;border-radius:12px;background:#fff}.toc-spec{display:inline-block;margin:4px 8px 0 0;font-size:12px;color:#475569}')
        parts.append('h2{margin:0 0 8px 0;font-size:20px}.class-section{margin-top:18px;padding:14px;border:1px solid #dbeafe;border-radius:16px;background:#f8fbff}.class-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline}')
        parts.append('h3{margin:14px 0 8px 0;font-size:15px;color:#0f172a}.spec-section{border-top:1px dashed #cbd5e1;margin-top:10px;padding-top:8px}')
        parts.append('.spell{margin-top:10px;padding:11px 13px;background:#fff;border:1px solid rgba(226,232,240,1);border-radius:12px;box-shadow:0 1px 2px rgba(15,23,42,.04)}')
        parts.append('.spell-head{display:flex;gap:8px;align-items:flex-start;font-weight:800}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#22c55e;margin-top:7px;flex:0 0 auto}')
        parts.append('.spell-title{font-weight:800}.subtle{color:#64748b;font-size:12px;font-weight:500}.line{margin-top:7px;color:#0f172a;font-size:13px;padding-left:17px}')
        parts.append('.hash{color:#2563eb;font-weight:800;margin-right:4px}.k{color:#334155;font-weight:800}.ins{color:#16a34a;font-weight:800}.del{color:#dc2626;font-weight:800;text-decoration:line-through}')
        parts.append('.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.hidden{display:none!important}@media(max-width:720px){body{padding:8px}.card{padding:14px}.hero{display:block}.controls{top:0}}')
        parts.append('</style>')
        parts.append('</head>')
        parts.append('<body>')
        parts.append('<div class="card">')
        parts.append('<div class="hero">')
        parts.append(f"<div><h1>{html.escape(server_title)} 职业技能变更报告：{html.escape(title_from)} → {html.escape(title_to)}</h1>")
        parts.append(f"<div class='meta'><span>语言：{html.escape(self.locale)}</span><span>数据版本：{html.escape(from_build)} → {html.escape(to_build)}</span></div></div>")
        if wowhead_url:
            parts.append(f"<div class='meta'><a href='{html.escape(wowhead_url)}' target='_blank' rel='noopener noreferrer'>Wowhead 参考链接</a></div>")
        parts.append('</div>')
        parts.append('<div class="summary">')
        parts.append(f"<div class='metric'><span>变更技能</span><strong>{len(spell_changes)}</strong></div>")
        parts.append(f"<div class='metric'><span>涉及职业</span><strong>{class_count}</strong></div>")
        parts.append(f"<div class='metric'><span>涉及专精</span><strong>{sum(class_spec_counts.values())}</strong></div>")
        parts.append(f"<div class='metric'><span>DB2 表</span><strong>{len(changed_table_counts)}</strong></div>")
        parts.append('</div>')
        if changed_table_counts:
            table_summary = '、'.join([f"{k} {v}" for k, v in sorted(changed_table_counts.items(), key=lambda x: (-x[1], x[0]))[:8]])
            parts.append(f"<div class='meta'><span>主要变更表：{html.escape(table_summary)}</span></div>")
        parts.append("<div class='controls'><input id='spellFilter' type='search' placeholder='筛选技能名、ID、职业、专精或 DB2 表…' autocomplete='off'><span class='count' id='filterCount'>全部显示</span></div>")

        parts.append("<div class='toc'><div class='toc-title'>目录</div>")
        for cid in sorted(class_to_spec_to_spells.keys()):
            cname = (display_class_names or {}).get(cid) or str(cid)
            parts.append(f"<div class='toc-item'><a href='#class-{cid}'>{html.escape(cname)}</a> <span class='subtle'>{class_spell_counts.get(cid, 0)} 技能 / {class_spec_counts.get(cid, 0)} 专精</span>")
            spec_map = class_to_spec_to_spells.get(cid) or {}
            for spec_id in sorted(spec_map.keys()):
                if spec_id == 0:
                    spec_name = '通用'
                else:
                    spec_name = ((display_spec_meta or {}).get(spec_id) or {}).get('name') or str(spec_id)
                parts.append(f"<a class='toc-spec' href='#class-{cid}-spec-{spec_id}'>{html.escape(spec_name)}({len(spec_map.get(spec_id) or [])})</a>")
            parts.append('</div>')
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
            cname = (display_class_names or {}).get(cid) or str(cid)
            parts.append(f"<section class='class-section' id='class-{cid}' data-class='{html.escape(str(cname).lower())}'>")
            parts.append(f"<div class='class-head'><h2>{html.escape(cname)}</h2><span class='subtle'>职业 {cid} ｜ {class_spell_counts.get(cid, 0)} 技能</span></div>")
            spec_map = class_to_spec_to_spells.get(cid) or {}
            for spec_id in sorted(spec_map.keys()):
                if spec_id == 0:
                    spec_name = '通用'
                else:
                    spec_name = ((display_spec_meta or {}).get(spec_id) or {}).get('name') or str(spec_id)
                parts.append(f"<section class='spec-section' id='class-{cid}-spec-{spec_id}'><h3>{html.escape(spec_name)} <span class='subtle'>专精 {spec_id} ｜ {len(spec_map.get(spec_id) or [])} 技能</span></h3>")
                for spell_id in sorted(spec_map.get(spec_id) or []):
                    sname = (zh_name_cache.get(spell_id) or '').strip() or (name_cache.get(spell_id) or '').strip() or str(spell_id)
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

                    search_text = ' '.join([str(sname), str(spell_id), str(cname), str(spec_name), ' '.join(diffs_by_table.keys())]).lower()
                    parts.append(f"<div class='spell' id='spell-{spell_id}' data-search='{html.escape(str(search_text), quote=True)}'>")
                    parts.append(f"<div class='spell-head'><span class='dot'></span><div><span class='spell-title'>{html.escape(sname)} <span class='subtle mono'>#{spell_id}</span></span>：{desc_primary} <a class='subtle' href='{html.escape(wowhead_spell_url)}' target='_blank' rel='noopener noreferrer'>Wowhead</a></div></div>")
                    for ln in lines:
                        parts.append(ln)
                    parts.append("</div>")
                parts.append('</section>')
            parts.append('</section>')
        parts.append("""<script>
(function(){
  var input=document.getElementById('spellFilter');
  var count=document.getElementById('filterCount');
  if(!input){return;}
  function apply(){
    var q=(input.value||'').trim().toLowerCase();
    var total=0, visible=0;
    document.querySelectorAll('.spell').forEach(function(el){
      total++;
      var ok=!q || (el.getAttribute('data-search')||'').indexOf(q)>=0 || (el.textContent||'').toLowerCase().indexOf(q)>=0;
      el.classList.toggle('hidden', !ok);
      if(ok){visible++;}
    });
    document.querySelectorAll('.spec-section').forEach(function(sec){
      var has=!!sec.querySelector('.spell:not(.hidden)');
      sec.classList.toggle('hidden', !has);
    });
    document.querySelectorAll('.class-section').forEach(function(sec){
      var has=!!sec.querySelector('.spell:not(.hidden)');
      sec.classList.toggle('hidden', !has);
    });
    if(count){count.textContent=q ? ('显示 '+visible+' / '+total) : ('全部 '+total+' 个技能');}
  }
  input.addEventListener('input', apply);
  apply();
})();
</script>""")
        parts.append('</div></body></html>')
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(parts))

        return {'path': rel_path, 'class_count': class_count}
