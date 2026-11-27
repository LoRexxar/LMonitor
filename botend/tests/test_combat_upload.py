"""独立上传页、浏览器本地分割和归档生命周期验收。"""
import uuid
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from botend.models import CombatLogCollection, CombatLogPolicy, CombatLogReport, CombatLogPurge, CombatPublicRolePolicy, DashboardUserGroup
from django.db import connection
from django.test.utils import CaptureQueriesContext
from botend.services import combat_log_archive as archive
from botend.tests.test_combat_logs import sample


class RetentionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(username="管理")
        self.owner = get_user_model().objects.create_user(username="成员")
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=self.root/"hot", COMBAT_LOG_COLD_ROOT=self.root/"cold"))
        self.client.force_login(self.admin)

    def test_policy_admission_publication_and_cleanup(self):
        url = reverse("dashboard") + "?section=combat-upload-policy"
        self.assertRedirects(self.client.get(reverse("combat_upload_policy")), url)
        self.assertTemplateUsed(self.client.get(url), "dashboard/index.html")
        for page in ("combat_logs", "combat_upload"):
            self.assertNotContains(self.client.get(reverse(page)), "combat-upload-policy")
            self.assertNotContains(self.client.get(reverse(page)), reverse("combat_upload_policy"))
        self.client.post(url, dict(account="", max_reports=9, retention_days=20))
        self.client.post(url, dict(account="成员", max_reports=1, retention_days=5, recycle_oldest="on"))
        self.assertEqual(archive.policy_for(self.admin).retention_days, 20)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(reverse("combat_upload_policy")).status_code, 403)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertNotContains(self.client.get(reverse("combat_logs")), 'type="file"')
        self.assertContains(self.client.get(reverse("combat_upload")), "5 天")
        raw = sample()
        old = archive.create(self.owner, "旧.txt", len(raw))
        self.assertTrue(timedelta(days=4) < old.expires_at-timezone.now() <= timedelta(days=5))
        archive.put_part(self.owner, old.pk, 0, raw)
        CombatLogReport.objects.filter(pk=old.pk).update(state="ready")
        hot = archive.directory(old)
        cold = archive.directory(old,"cold")
        (cold/"archive.zip").write_bytes(b"x")
        key = uuid.uuid4()
        batch = [("替换.txt",len(raw),key)]
        new = archive.create_batch(self.owner,batch)[0]
        self.assertEqual(archive.create_batch(self.owner,batch)[0].pk,new.pk)
        self.assertTrue(hot.is_dir() and cold.is_dir())
        with self.assertRaises(ValueError):
            archive.create(self.owner,"超额.txt",100)
        archive.put_part(self.owner,new.pk,0,raw)
        archive.enqueue(self.owner,new.pk)
        new.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(archive.collect(new.pk,new.attempt))
        self.assertFalse(hot.exists() or cold.exists())
        self.assertFalse(CombatLogReport.objects.filter(pk=old.pk).exists())
        new_hot = archive.directory(new)
        CombatLogReport.objects.filter(pk=new.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.client.get(reverse("combat_report",args=[new.pk])).status_code,404)
        with self.captureOnCommitCallbacks(execute=True):
            archive.expire_reports()
        self.assertFalse(new_hot.exists())
        self.assertFalse(CombatLogPurge.objects.exists())

    def test_reject_full_and_inherit(self):
        CombatLogPolicy.objects.create(scope="default",max_reports=1)
        first = archive.create(self.owner,"第一.txt",512)
        with self.assertRaises(ValueError):
            archive.create(self.owner,"第二.txt",512)
        self.assertTrue(CombatLogReport.objects.filter(pk=first.pk).exists())
        response = self.client.post(reverse("combat_upload_policy"), dict(account="成员",max_reports=5,retention_days=60))
        self.assertEqual(response.status_code,302)
        self.client.post(reverse("combat_upload_policy"), dict(account="成员",max_reports=5,retention_days=60,inherit="1"))
        self.assertEqual(archive.policy_for(self.owner).max_reports,1)

    def test_resume_key_is_scoped_to_site_account(self):
        batch = [("场次.txt", 1024, uuid.uuid4())]
        first, = archive.create_batch(self.owner, batch)
        second, = archive.create_batch(self.admin, batch)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(archive.create_batch(self.owner, batch)[0].pk, first.pk)
        self.assertEqual(archive.create_batch(self.admin, batch)[0].pk, second.pk)

    def test_merged_upload_rejects_invalid_or_changed_selection(self):
        scene = dict(key=str(uuid.uuid4()), name="合并.txt", size=30)
        body = dict(source_filename="战斗记录.txt", scenes=[scene])
        url = reverse("combat_upload")
        for selection in ([[0, 10, 10]], [[0, 10, 10], [9, 20, 20]], [[0, 10, 11], [15, 30, 20]]):
            with self.subTest(selection=selection):
                self.assertEqual(self.client.post(url, {**body, "selection": selection},
                                                  content_type="application/json").status_code, 400)
        self.assertFalse(CombatLogReport.objects.exists())
        body["selection"] = [[0, 10, 10], [15, 30, 20]]
        created = self.client.post(url, body, content_type="application/json")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.client.post(url, body, content_type="application/json").json(), created.json())
        body["selection"][1][1] += 1
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 400)
        self.assertEqual(CombatLogReport.objects.count(), 1)

    def test_legacy_resume_opens_local_scene_selection(self):
        old = archive.create(self.admin, "旧原文件.txt", 2000)
        url = reverse("combat_upload")
        response = self.client.get(url, {"resume": old.pk})
        self.assertContains(response, "旧版整文件上传记录")
        self.assertContains(response, 'value="manual"')
        self.assertNotContains(response, "data-resume=")
        current, = archive.create_batch(self.admin, [("团本.txt", 500, uuid.uuid4())])
        self.assertContains(self.client.get(url, {"resume": current.pk}),
                            'data-resume="' + reverse("combat_report", args=[current.pk]) + '"')

    @override_settings(COMBAT_LOG_MAX_SOURCE_BYTES=4096, COMBAT_LOG_MAX_BYTES=2048)
    def test_source_limit_and_extracted_report_limit_are_independent(self):
        self.assertContains(self.client.get(reverse("combat_upload")), 'data-max-source-bytes="4096"')
        archive.validate_file("首领.txt", 2048)
        with self.assertRaises(ValueError):
            archive.validate_file("首领.txt", 2049)

    def test_removing_account_discards_only_its_policy_overrides(self):
        for model in (CombatLogPolicy, CombatPublicRolePolicy):
            for scope in ("default", str(self.admin.pk), str(self.owner.pk)):
                model.objects.create(scope=scope)
        self.owner.delete()
        for model in (CombatLogPolicy, CombatPublicRolePolicy):
            self.assertSetEqual(set(model.objects.values_list("scope", flat=True)), {"default", str(self.admin.pk)})

    def test_library_query_count_independent_of_reports_and_collections(self):
        url = reverse("combat_logs") + "?source=uploads"
        def render_count():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
                self.assertContains(response, self.admin.username)
            return len(queries)
        archive.create(self.admin, "第一份.txt", 100)
        CombatLogCollection.objects.create(owner=self.admin, title="第一册")
        baseline = render_count()
        for i in range(4):
            archive.create(self.admin, f"团本{i}.txt", 100)
            CombatLogCollection.objects.create(owner=self.admin, title=f"团本{i}")
        self.assertEqual(render_count(), baseline)

    def test_publication_rechecks_quota_after_reserving_replacement(self):
        policy = CombatLogPolicy.objects.create(scope=str(self.owner.pk), max_reports=1, recycle_oldest=True)
        old = archive.create(self.owner, "原日志.txt", 10)
        CombatLogReport.objects.filter(pk=old.pk).update(state="ready")
        raw = sample()
        new = archive.create(self.owner, "新日志.txt", len(raw))
        old.refresh_from_db()
        self.assertEqual(old.retire_after_id, new.pk)
        policy.max_reports = 2
        policy.save()
        archive.put_part(self.owner, new.pk, 0, raw)
        archive.enqueue(self.owner, new.pk)
        new.refresh_from_db()
        archive.collect(new.pk, new.attempt)
        new.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(new.state, "ready")
        self.assertIsNone(old.retire_after_id)

    def test_expired_upload_key_can_be_reused_before_scheduled_cleanup(self):
        CombatLogPolicy.objects.create(scope=str(self.owner.pk), max_reports=1)
        batch = [("日志.txt", 10, uuid.uuid4())]
        first, = archive.create_batch(self.owner, batch)
        CombatLogReport.objects.filter(pk=first.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        second, = archive.create_batch(self.owner, batch)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(archive.create_batch(self.owner, batch)[0].pk, second.pk)
        self.assertTrue(CombatLogReport.objects.filter(pk=first.pk, upload_key=None).exists())

    def test_maintenance_reclaims_only_expired_benchmark_files(self):
        folder = self.root / "hot" / "public-benchmarks"
        folder.mkdir(parents=True)
        for name in ("a" * 64 + ".json", "b" * 64 + ".json", ".pending-stale.json", "保留.json"):
            path = folder / name
            path.write_text("{}", encoding="utf-8")
            if name != "b" * 64 + ".json":
                before = time.time() - 90000
                os.utime(path, (before, before))
        archive.expire_reports()
        self.assertEqual({p.name for p in folder.iterdir()}, {"b" * 64 + ".json", "保留.json"})

    def test_same_group_accounts_apply_independent_upload_policies(self):
        other = get_user_model().objects.create_user(username="同组成员")
        group = DashboardUserGroup.objects.create(name="内容编辑", permission_codes=["news.index"])
        group.users.add(self.owner, other)
        url = reverse("dashboard") + "?section=combat-upload-policy"
        for scope, days, action in [("default", 30, "reject"), (str(self.owner.pk), 7, "recycle")]:
            response = self.client.post(url, {"scope": scope, "max_reports": 1,
                                              "retention_days": days, "full_action": action})
            self.assertEqual(response.status_code, 302)

        # 先创建同组成员的日志，确认按创建时间回收时也只在上传者自己的日志中选择。
        start = timezone.now()
        other_log = archive.create(other, "同组成员.txt", 100)
        own_log = archive.create(self.owner, "成员.txt", 100)
        for report, days in [(other_log, 30), (own_log, 7)]:
            self.assertGreaterEqual(report.expires_at, start + timedelta(days=days))
            self.assertLessEqual(report.expires_at, timezone.now() + timedelta(days=days))
        CombatLogReport.objects.filter(pk__in=[other_log.pk, own_log.pk]).update(state="ready")
        with self.assertRaisesMessage(ValueError, "已达到账号的 1 份上限"):
            archive.create(other, "再次上传.txt", 100)
        group.users.remove(self.owner)
        replacement = archive.create(self.owner, "替换.txt", 100)
        own_log.refresh_from_db()
        other_log.refresh_from_db()
        self.assertEqual(own_log.retire_after_id, replacement.pk)
        self.assertIsNone(other_log.retire_after_id)
        self.assertEqual(archive.policy_for(self.owner).retention_days, 7)

    def test_policy_table_search_pagination_and_scope(self):
        users = get_user_model().objects
        users.bulk_create([get_user_model()(username=f"测试账号{i:02d}") for i in range(27)])
        url = reverse("dashboard") + "?section=combat-upload-policy&q=测试账号&page=2"
        response = self.client.get(url)
        self.assertEqual([row["label"] for row in response.context["policy_rows"]],
                         ["全站默认", "测试账号25", "测试账号26"])
        account = users.get(username="测试账号26")
        values = {"scope": str(account.pk), "max_reports": 7, "retention_days": 12, "full_action": "recycle"}
        response = self.client.post(url, values, follow=True)
        self.assertEqual(response.context["policy_page"].number, 2)
        self.assertEqual(response.context["policy_query"], "测试账号")
        self.assertTrue(response.context["policy_rows"][-1]["custom"])
        self.assertTrue(archive.policy_for(account).recycle_oldest)
        for invalid in ({"scope": ""}, {"scope": "12345678"}, {"scope": "default", "inherit": "1"},
                        {"retention_days": 0}, {"full_action": "invalid"}):
            self.assertIn(self.client.post(url, {**values, **invalid}).status_code, (400, 404))
        self.assertFalse(CombatLogPolicy.objects.filter(scope="default").exists())
        self.assertContains(self.client.get(url.replace("测试账号", "不存在")), "没有匹配的账号")

    def test_account_usage_counts_reserved_space_and_only_lists_owned_records(self):
        url = reverse("combat_upload_usage", args=[self.owner.pk])
        self.assertContains(self.client.get(url), "该账号当前没有日志")
        archive.create(self.admin, "别人的日志.txt", 100)
        original = archive.create(self.owner, "待回收.txt", 800)
        current = archive.create(self.owner, "正在上传.txt", 1000)
        CombatLogReport.objects.filter(pk=original.pk).update(retire_after=current, state="ready")
        CombatLogReport.objects.filter(pk=current.pk).update(state="paused", reserved_bytes=200)
        response = self.client.get(url)
        self.assertEqual((response.context["slots"], response.context["remaining"], response.context["reserved"]),
                         (1, 9, 1000))
        self.assertContains(response, "待替换回收")
        self.assertContains(response, "暂停上传")
        self.assertNotContains(response, "别人的日志")
        CombatLogReport.objects.bulk_create([
            CombatLogReport(owner=self.owner, filename=f"日志{i}.txt", size=1, part_size=1) for i in range(10)
        ])
        page_two = self.client.get(url, {"page": 2})
        self.assertContains(page_two, "第 2 / 2 页")
        self.assertContains(page_two, "正在上传.txt")
        self.assertEqual(page_two.context["remaining"], 0)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)


class RecyclingLifecycleTests(TransactionTestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root / "hot", COMBAT_LOG_COLD_ROOT=root / "cold"))
        self.owner = get_user_model().objects.create_user(username="回收验收")

    def upload(self, name, raw):
        report = archive.create(self.owner, name, len(raw))
        archive.put_part(self.owner, report.pk, 0, raw)
        self.client.force_login(self.owner)
        with patch("subprocess.Popen") as shell:
            self.assertEqual(self.client.post(reverse("combat_finish", args=[report.pk])).status_code, 200)
            thread = archive._index_thread
            if thread is not None:
                thread.join(30)
                self.assertFalse(thread.is_alive())
            shell.assert_not_called()
        return report

    def test_upload_recycles_only_after_success_and_preserves_old_on_failure(self):
        from botend.management.commands.combat_log_storage import transfer

        CombatLogPolicy.objects.create(scope=str(self.owner.pk), max_reports=1, recycle_oldest=True)
        old = self.upload("旧日志.txt", sample())
        old.refresh_from_db()
        self.assertEqual(old.state, "ready")
        transfer(old.pk, "cold")
        hot, cold = archive.directory(old, create=False), archive.directory(old, "cold", create=False)
        self.assertTrue((cold / "archive.zip").is_file())

        with self.assertLogs(archive.logger, level="ERROR"):
            failed = self.upload("无效日志.txt", b"invalid")
        failed.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(failed.state, "failed")
        self.assertEqual(old.retire_after_id, failed.pk)
        self.assertEqual(b"".join(archive.archived_chunks(old)), sample())
        self.client.force_login(self.owner)
        self.client.post(reverse("combat_remove", args=[failed.pk]))
        old.refresh_from_db()
        self.assertIsNone(old.retire_after_id)

        self.assertTrue(hot.exists() and cold.exists())
        new = self.upload("新日志.txt", sample())
        new.refresh_from_db()
        self.assertEqual(new.state, "ready")
        self.assertFalse(CombatLogReport.objects.filter(pk=old.pk).exists())
        self.assertFalse(hot.exists() or cold.exists())
        self.assertFalse(CombatLogPurge.objects.exists())

    def test_original_maintenance_expires_logs_and_retries_file_cleanup(self):
        from botend.services import snapshot_cache_maintenance as maintenance

        report = self.upload("到期日志.txt", sample())
        hot = archive.directory(report, create=False)
        cold = archive.directory(report, "cold")
        self.enterContext(patch.object(maintenance, "maintain_snapshot_caches", return_value=[]))
        self.enterContext(patch("botend.services.storage_lifecycle_maintenance.run_extended_maintenance", return_value=[]))
        self.enterContext(self.settings(VARIANT_TEXT_MAINTENANCE_ENABLED=False, SPEC_AGGREGATION_WORK_GC_ENABLED=False))
        CombatLogReport.objects.filter(pk=report.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch.object(archive.shutil, "rmtree", side_effect=PermissionError("模拟文件占用")), self.assertLogs(level="ERROR"):
            results = maintenance.run_maintenance_once()
        self.assertEqual(next(r["deleted"] for r in results if r["kind"] == "combat_logs"), 1)
        self.assertFalse(CombatLogReport.objects.filter(pk=report.pk).exists())
        self.assertTrue(CombatLogPurge.objects.filter(pk=report.pk).exists())
        self.assertTrue(hot.exists())
        maintenance.run_maintenance_once()
        self.assertFalse(hot.exists() or cold.exists())
        self.assertFalse(CombatLogPurge.objects.exists())


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class UploadBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def _make_connections_override(cls):
        return {}

    def wait_for_indexes(self):
        thread = archive._index_thread
        if thread is not None:
            thread.join(90)
            self.assertFalse(thread.is_alive())

    def test_upload_unsettled_keys_after_exit_and_at_eof(self):
        from playwright.sync_api import expect, sync_playwright
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold"))
        self.client.force_login(get_user_model().objects.create_user(username="未结算钥匙"))
        events = [
            ('01.9308', 'CHALLENGE_MODE_START,"副本甲",2660,503,12,[]'),
            ('02.1208', 'COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.5,PROJECT_ID,1'),
            ('05.0108', 'SPELL_CAST_SUCCESS,Player-1,"队员",0,0,Creature-1,"敌人",0,0,11,"技能",4'),
            ('06.0208', 'ZONE_CHANGE,2660,"OUTSIDE",23'),
            ('07.0208', 'SPELL_CAST_SUCCESS,OUTSIDE'),
            ('08.9308', 'CHALLENGE_MODE_START,"副本乙",2660,503,21,[]'),
            ('29.9308', 'COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.5,PROJECT_ID,1'),
            ('29.9318', 'MAP_CHANGE,1234,"副本乙",1,2,3,4'),
            ('29.9328', 'COMBATANT_INFO,Player-1,0,63,[]'),
            ('35.0108', 'ENCOUNTER_START,2926,"蛇神",8,5,2660'),
            ('39.0108', 'SPELL_CAST_SUCCESS,Player-1,"队员",0,0,Creature-1,"敌人",0,0,11,"技能",4'),
            ('45.0108', 'ENCOUNTER_END,2926,"蛇神",8,5,0'),
        ]
        data = '\n'.join(f'10/26/2026 00:15:{sec}  {event}' for sec, event in events).encode()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.context.add_cookies([dict(name="sessionid", value=self.client.cookies["sessionid"].value,
                                               url=self.live_server_url)])
                page.goto(self.live_server_url+reverse("combat_upload"))
                page.locator("#source-file").set_input_files(dict(name="两把钥匙.txt",mimeType="text/plain",buffer=data))
                rows = page.locator("#scene-list label")
                expect(rows).to_have_count(2)
                expect(rows.nth(0)).to_contain_text("未结算（离开副本")
                expect(rows.nth(1)).to_contain_text("未结算（文件结束")
                expect(rows.nth(1)).to_contain_text("首领交战：蛇神")
                expect(rows.nth(0)).to_contain_text("副本甲 +12")
                expect(rows.nth(1)).to_contain_text("副本乙 +21")
                page.locator("#send-log").click()
                expect(page.locator("#upload-status")).to_contain_text("上传完成")
            finally:
                browser.close()
        self.wait_for_indexes()
        reports = list(CombatLogReport.objects.order_by("created_at"))
        self.assertEqual(len(reports), 2)
        for report, duration in zip(reports, (3080, 36080)):
            self.assertEqual(report.source_filename, "两把钥匙.txt")
            self.assertEqual(report.state, "ready", report.message)
            fight, *children = report.summary["activity_index"]
            self.assertEqual(fight["level"], "12" if report == reports[0] else "21")
            if report == reports[1]:
                boss, = children
                self.assertEqual((boss["name"], boss["parent"]), ("蛇神", fight["id"]))
            self.assertEqual(fight["result"], "未结算")
            self.assertEqual(fight["observed_end_ms"] - fight["start_ms"], duration)
            self.assertNotIn(b"OUTSIDE", b"".join(archive.archived_chunks(report)))
            page = self.client.get(reverse("combat_analysis", args=[report.pk]),
                                   {"fight": fight["id"], "wave": "all", "metric": "casts"})
            self.assertEqual(page.context["total"], 1)
            self.assertEqual(page.context["rate"], round(1000/duration, 2))

    def test_merge_selected_unfinished_scenes_and_resume_as_one_report(self):
        from playwright.sync_api import expect, sync_playwright
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold"))
        self.enterContext(patch.object(archive, "PART_SIZE", 96))
        owner = get_user_model().objects.create_user(username="合并上传")
        self.client.force_login(owner)
        CombatLogPolicy.objects.create(scope="default", max_reports=1)
        events = [
            "COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.5,PROJECT_ID,1",
            'CHALLENGE_MODE_START,"未结算的钥匙",2660,503,12,[]',
            'SPELL_CAST_SUCCESS,Player-1,"队员",0,0,Creature-1,"敌人",0,0,11,"技能",4',
            'ZONE_CHANGE,1,"排除野外",0',
            'ENCOUNTER_START,399,"排除未选首领",16,20,900',
            'ENCOUNTER_END,399,"排除未选首领",16,20,1',
            'ENCOUNTER_START,400,"未结算的团本首领",16,20,900',
            'SPELL_CAST_SUCCESS,Player-2,"队员乙",0,0,Creature-2,"敌人乙",0,0,12,"技能乙",4',
            'ENCOUNTER_START,401,"排除世界首领",0,20,1',
            'SPELL_CAST_SUCCESS,OUTSIDE',
            'ZONE_CHANGE,2660,"排除普通地下城",23',
            'SPELL_DAMAGE,Player-1,"队员",0,0,Creature-1,"排除小怪",0,0,20,"平砍",1,100',
        ]
        rows = [f"10/26/2026 00:15:{i:02}.0008  {event}\n".encode() for i,event in enumerate(events)]
        data = b"".join(rows)
        payload = dict(name="开荒记录.txt", mimeType="text/plain", buffer=data)
        with ThreadPoolExecutor(max_workers=1) as checks, sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                cookie = dict(name="sessionid", value=self.client.cookies["sessionid"].value, url=self.live_server_url)
                page.context.add_cookies([cookie])
                url = self.live_server_url + reverse("combat_upload")
                page.goto(url)
                page.locator("#source-file").set_input_files(payload)
                listing = page.locator("#scene-list label")
                expect(listing).to_have_count(3)
                expect(page.locator("#send-log")).to_be_disabled()
                page.get_by_label("手动选择", exact=False).check()
                expect(listing).to_have_count(4)
                for label in ("未结算的团本首领", "未结算的钥匙"):
                    listing.filter(has_text=label).locator("input").check()
                expect(listing.filter(has_text="未结算的团本首领")).to_contain_text("部分记录（下一场开始")
                expect(page.locator("#selection-total")).to_contain_text("2 场 · 1 份日志")
                page.route("**/parts/1/", lambda route: route.abort())
                page.locator("#send-log").click()
                expect(page.locator("#send-log")).to_be_enabled()
                report = checks.submit(CombatLogReport.objects.get).result()
                self.assertEqual(checks.submit(report.parts.count).result(), 1)
                # 换到没有本地存储的新浏览器上下文，从日志库的续传入口恢复原选区。
                resume = browser.new_page()
                resume.context.add_cookies([cookie])
                resume.goto(url + f"?resume={report.pk}")
                resume.locator("#source-file").set_input_files({**payload, "buffer": b"".join(rows[:6])})
                expect(resume.locator("#upload-status")).to_contain_text("不匹配")
                expect(resume.locator("#send-log")).to_be_disabled()
                resume.locator("#source-file").set_input_files(payload)
                expect(resume.locator("#scene-list label")).to_have_count(2)
                parts = []
                resume.on("request", lambda r: parts.append(urlsplit(r.url).path) if r.method == "PUT" else None)
                resume.locator("#send-log").click()
                expect(resume.locator("#upload-status")).to_contain_text("上传完成")
                self.assertEqual(parts, [reverse("combat_part", args=[report.pk, i])
                                         for i in range(1, archive.part_count(report))])
                self.wait_for_indexes()
            finally:
                browser.close()
        report = CombatLogReport.objects.get()
        self.assertEqual((report.state, report.source_filename), ("ready", payload["name"]), report.message)
        expected = b"".join([rows[0], *rows[1:3], rows[0], *rows[6:8]])
        self.assertEqual(report.size, len(expected))
        self.assertEqual(b"".join(archive.archived_chunks(report)), expected)
        selection = report.summary["upload_selection"]
        self.assertEqual([f["result"] for f in report.summary["activity_index"]], ["未结算", "终点缺失"])
        library = self.client.get(reverse("combat_logs"), {"source": "uploads"})
        self.assertContains(library, report.filename)
        directory = self.client.get(reverse("combat_battles"), {"log": str(report.pk)})
        self.assertEqual((directory.context["report_count"], directory.context["pull_count"]), (1, 2))
        with patch.object(archive, "launch", side_effect=archive.collect):
            archive.enqueue(owner, report.pk, rebuild=True)
        report.refresh_from_db()
        self.assertEqual((report.state, report.summary["upload_selection"]), ("ready", selection))
        self.assertEqual(len(report.summary["activity_index"]), 2)

    def test_public_role_admin_control_alignment(self):
        from playwright.sync_api import expect, sync_playwright
        from botend.models import CombatPublicRolePolicy

        admin = get_user_model().objects.create_superuser(username="名额管理")
        CombatPublicRolePolicy.objects.create(scope=str(admin.pk), limit=7)
        self.client.force_login(admin)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page(reduced_motion="reduce")
                # 管理表和编辑窗口使用站内静态资源，隔离公共 CDN 的响应时间。
                page.route("https://**/*", lambda route: route.abort())
                page.context.add_cookies([{"name":"sessionid", "value":self.client.cookies["sessionid"].value,
                                           "url":self.live_server_url}])
                page.goto(self.live_server_url + reverse("dashboard") + f"?section=combat-public-roles&saved={admin.pk}",
                          wait_until="domcontentloaded")
                area = page.locator("#combat-public-roles")
                edit = area.locator(f'[data-role-edit="{admin.pk}"]')
                for width in (1366, 390):
                    page.set_viewport_size({"width":width, "height":810})
                    expect(edit).to_be_visible()
                    differences = area.evaluate("""section => {
                        const rect = s => section.querySelector(s).getBoundingClientRect();
                        const search = rect('input[type=search]'), submit = rect('.policy-toolbar button');
                        const edits = [...section.querySelectorAll('[data-role-edit]')].map(b => b.getBoundingClientRect().right);
                        return [search.top - submit.top, search.height - submit.height,
                                Math.max(...edits) - Math.min(...edits), submit.right - edits[0]];
                    }""")
                    self.assertTrue(all(abs(n) <= 1 for n in differences), (width, differences))
                    edit.click()
                    dialog = area.get_by_role("dialog")
                    restore = dialog.get_by_role("checkbox", name="恢复全站默认")
                    restore.check()
                    expect(dialog.locator('[name="limit"]')).to_be_disabled()
                    differences = dialog.evaluate("""d => {
                        const rect = s => d.querySelector(s).getBoundingClientRect();
                        const cancel = rect('[data-close]'), save = rect('[type=submit]');
                        const box = rect('[name=inherit]'), label = rect('.policy-inherit');
                        return [cancel.top-save.top, cancel.width-save.width, cancel.height-save.height,
                                save.right-rect('[name=limit]').right, (box.top+box.bottom-label.top-label.bottom)/2];
                    }""")
                    self.assertTrue(all(abs(n) <= 1 for n in differences), (width, differences))
                    dialog.get_by_role("button", name="取消").click()
                    expect(edit).to_be_focused()
            finally:
                browser.close()

    def test_admin_edits_account_policy_rows(self):
        from playwright.sync_api import expect, sync_playwright
        users = get_user_model().objects
        admin = users.create_superuser(username="管理")
        owner = users.create_user(username="成员甲")
        other = users.create_user(username="成员乙")
        self.client.force_login(admin)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.context.add_cookies([{"name": "sessionid", "value": self.client.cookies["sessionid"].value,
                                           "url": self.live_server_url}])
                page.route("https://**/*", lambda route: route.abort())
                page.goto(self.live_server_url + reverse("dashboard") + "?section=combat-upload-policy")
                default = page.locator("#policy-row-default")
                custom = page.locator(f"#policy-row-{owner.pk}")
                inherited = page.locator(f"#policy-row-{other.pk}")
                expect(custom).to_contain_text("跟随默认")
                dialog = page.get_by_role("dialog")
                edit = custom.get_by_role("button", name="编辑成员甲的设置")
                expect(custom.locator("input, select")).to_have_count(0)
                custom.get_by_text("跟随默认").click()
                details = page.get_by_role("region", name="成员甲的使用详情")
                expect(details).to_contain_text("该账号当前没有日志")
                other_toggle = inherited.get_by_role("button", name="查看成员乙的使用详情")
                other_toggle.focus()
                page.keyboard.press("Enter")
                expect(details).not_to_be_visible()
                expect(other_toggle).to_have_attribute("aria-expanded", "true")
                other_toggle.click()
                expect(other_toggle).to_have_attribute("aria-expanded", "false")
                edit.click()
                expect(details).not_to_be_visible()
                expect(dialog.get_by_role("heading")).to_have_text("编辑账号 · 成员甲")
                limit = dialog.get_by_label("日志上限（份）")
                days = dialog.get_by_label("有效期（天）")
                action = dialog.get_by_role("combobox")
                save = dialog.get_by_role("button", name="保存", exact=True)
                limit.fill("99")
                page.keyboard.press("Escape")
                expect(dialog).not_to_be_visible()
                expect(edit).to_be_focused()
                edit.click()
                expect(limit).to_have_value("10")
                limit.fill("6")
                days.fill("45")
                action.select_option("recycle")
                save.click()
                expect(dialog).not_to_be_visible()
                expect(custom).to_contain_text("独立设置")
                expect(custom).to_contain_text("45 天")
                default.get_by_role("button").click()
                expect(dialog.get_by_role("heading")).to_have_text("编辑全站默认设置")
                limit.fill("18")
                days.fill("90")
                save.click()
                expect(inherited).to_contain_text("18 份")
                expect(custom).to_contain_text("6 份")
                expect(custom).to_contain_text("回收最旧日志")
                page.set_viewport_size({"width": 390, "height": 844})
                edit.click()
                restore = dialog.get_by_role("checkbox", name="恢复全站默认")
                restore.check()
                expect(days).to_have_value("90")
                expect(limit).to_be_disabled()
                restore.uncheck()
                expect(days).to_have_value("45")
                dialog.get_by_role("button", name="取消").click()
                expect(custom).to_contain_text("独立设置")
                edit.click()
                restore.check()
                save.click()
                expect(custom).to_contain_text("已恢复全站默认")
                expect(custom).to_contain_text("90 天")
                expect(custom).to_contain_text("拒绝新上传")
            finally:
                browser.close()

    def test_two_gib_select_one_real_keystone(self):
        from playwright.sync_api import expect, sync_playwright
        source = os.environ.get("LMONITOR_REPLAY_121_FIXTURE")
        if not source:
            self.skipTest("未指定 12.1 实际回放样本")
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold"))
        owner = get_user_model().objects.create_user(username="大文件验收")
        self.client.force_login(owner)
        writes = []
        def record_write(request):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                writes.append((request.method, urlsplit(request.url).path,
                               int(request.all_headers().get("content-length", 0))))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.context.add_cookies([{"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url}])
                page.goto(self.live_server_url + reverse("combat_upload"))
                page.get_by_label("手动选择", exact=False).check()
                page.context.on("request", record_write)
                page.locator("#source-file").set_input_files(source)
                source_input = page.locator("#source-file")
                source_input.evaluate("""input => {
                    const log = input.files[0], size = 2 ** 31;
                    const parts = Array(Math.floor(size / log.size)).fill(log);
                    parts.push(log.slice(0, size % log.size));
                    const files = new DataTransfer();
                    files.items.add(new File([...parts, new Uint8Array(1)], '超限.txt'));
                    input.files = files.files;
                    input.dispatchEvent(new Event('change'));
                }""")
                expect(page.locator("#upload-status")).to_contain_text("原文件超过本地扫描上限")
                expect(page.locator("#send-log")).to_be_disabled()
                source_input.evaluate("""input => {
                    const exact = input.files[0].slice(0, 2 ** 31);
                    const files = new DataTransfer();
                    files.items.add(new File([exact], '2GiB.txt'));
                    input.files = files.files;
                    input.dispatchEvent(new Event('change'));
                }""")
                expect(page.locator("#upload-status")).to_contain_text("扫描完成", timeout=90000)
                self.assertGreater(page.locator("#scene-list input").count(), 400)
                page.locator("#scene-list label").filter(has_text="Voidscar Arena").first.locator("input").check()
                self.assertEqual(writes, [])
                page.locator("#send-log").click()
                expect(page.locator("#uploaded a")).to_have_count(1,timeout=90000)
                self.wait_for_indexes()
            finally:
                browser.close()
        report = CombatLogReport.objects.get()
        self.assertEqual(report.state, "ready")
        self.assertEqual(report.size, 9_794_058)
        base = reverse("combat_report", args=[report.pk])
        self.assertEqual(writes[0][:2], ("POST", reverse("combat_upload")))
        self.assertLess(writes[0][2], 1024)
        parts = [("PUT", f"{base}parts/{i}/", min(report.part_size, report.size-offset))
                 for i, offset in enumerate(range(0, report.size, report.part_size))]
        self.assertEqual(writes[1:], [*parts, ("POST", base+"finish/", 0)])
        self.assertLess(report.size, Path(source).stat().st_size)
        self.assertGreater(report.size, 9 * 1024**2)
        fights = report.summary["activity_index"]
        self.assertEqual([(f["kind"],f["name"]) for f in fights], [("钥匙","Voidscar Arena")])
        contents = b"".join(archive.archived_chunks(report))
        self.assertNotIn(b"The Blinding Vale", contents)
        self.assertIn(b"COMBATANT_INFO", contents)

    def test_select_then_upload_all(self):
        from playwright.sync_api import expect, sync_playwright
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot",COMBAT_LOG_COLD_ROOT=root/"cold"))
        self.enterContext(patch.object(archive,"PART_SIZE",96))
        owner = get_user_model().objects.create_user(username="上传验收")
        self.client.force_login(owner)
        data = sample()+b'\n'+b'\n'.join([
            b'9/30/2026 19:00:00.000-4  SPELL_CAST_SUCCESS,OUTDOOR_ENTRY',
            b'9/30/2026 19:10:00.000-4  ENCOUNTER_START,400,"Raid boss",16,20,900',
            b'9/30/2026 19:12:00.000-4  ENCOUNTER_END,400,"Raid boss",16,20,1',
            b'9/30/2026 19:20:00.000-4  SPELL_CAST_SUCCESS,OUTDOOR_ENTRY',
            b'9/30/2026 19:21:00.000-4  ZONE_CHANGE,2660,"NORMAL_ONLY",23',
            b'9/30/2026 19:22:00.000-4  SPELL_DAMAGE,Player-1,"p",0,0,Creature-1,"NORMAL_ONLY",0,0,20,"hit",1,100'])
        with ThreadPoolExecutor(max_workers=1) as checks, sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page=browser.new_context().new_page()
                page.context.add_cookies([{"name":"sessionid","value":self.client.cookies["sessionid"].value,"url":self.live_server_url}])
                page.goto(self.live_server_url+reverse("combat_upload"))
                page.get_by_label("手动选择", exact=False).check()
                page.locator("#source-file").set_input_files(dict(name="whole.txt",mimeType="text/plain",buffer=data))
                expect(page.locator("#scene-list input")).to_have_count(3)
                self.assertFalse(checks.submit(CombatLogReport.objects.exists).result())
                page.locator("#scene-list label").filter(has_text="Raid boss").locator("input").check()
                # 上传一片后模拟网络中断，重新提交同一场不多占份数。
                page.route("**/parts/1/",lambda route:route.abort())
                page.locator("#send-log").click()
                expect(page.locator("#send-log")).to_be_enabled()
                self.assertEqual(checks.submit(lambda: CombatLogReport.objects.get().parts.count()).result(),1)
                page.unroute("**/parts/1/")
                def pause_before_manifest(route):
                    page.locator("#stop-log").click()
                    route.continue_()
                page.route("**/parts/", pause_before_manifest, times=1)
                page.locator("#send-log").click()
                expect(page.locator("#upload-status")).to_contain_text("上传已暂停")
                paused = checks.submit(CombatLogReport.objects.get).result()
                self.assertEqual((paused.state, paused.reserved_bytes), ("paused", paused.part_size))
                resume = page.context.new_page()
                resume.goto(self.live_server_url + reverse("combat_upload") + f"?resume={paused.pk}")
                resumed_parts = []
                resume.on("request", lambda r: resumed_parts.append(urlsplit(r.url).path) if r.method == "PUT" else None)
                resume.locator("#source-file").set_input_files(dict(name="whole.txt", mimeType="text/plain", buffer=data))
                expect(resume.locator("#scene-list label")).to_have_count(1)
                expect(resume.locator("#scene-list")).to_contain_text("Raid boss")
                self.assertEqual(resumed_parts, [])
                resume.locator("#send-log").click()
                expect(resume.locator("#upload-status")).to_contain_text("上传完成")
                self.assertEqual(resumed_parts, [reverse("combat_part", args=[paused.pk, i])
                                                 for i in range(1, archive.part_count(paused))])
                resume.close()
                self.wait_for_indexes()
                report=checks.submit(CombatLogReport.objects.get).result()
                self.assertEqual(report.state,"ready")
                content=checks.submit(lambda:b"".join(archive.archived_chunks(report))).result()
                self.assertIn(b'Raid boss',content)
                self.assertNotIn(b'OUTDOOR_ENTRY',content)
                self.assertNotIn(b'NORMAL_ONLY',content)
                self.assertNotIn(b'CHALLENGE_MODE_START',content)
                page.get_by_label("全部副本场次",exact=False).check()
                expect(page.locator("#scene-list input")).to_have_count(2)
                page.locator("#send-log").click()
                expect(page.locator("#uploaded a")).to_have_count(2)
                self.wait_for_indexes()
                self.assertEqual(checks.submit(lambda: CombatLogReport.objects.filter(state="ready").count()).result(),2)
                key=checks.submit(lambda: CombatLogReport.objects.exclude(pk=report.pk).get()).result()
                self.assertEqual(len(key.summary["activity_index"]),3)
                content=checks.submit(lambda:b"".join(archive.archived_chunks(key))).result()
                self.assertNotIn(b'OUTDOOR_ENTRY',content)
                self.assertNotIn(b'Raid boss',content)
                self.assertNotIn(b'NORMAL_ONLY',content)
            finally:
                browser.close()
