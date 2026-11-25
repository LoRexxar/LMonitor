import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from botend.models import CombatLogReport
from botend.services import combat_log_archive as archive
from botend.services.combat_log_index import build_index, iter_lines


def sample():
    records = [
        "COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.0,PROJECT_ID,1",
        "CHALLENGE_MODE_END,2660,0,0,0",
        'CHALLENGE_MODE_START,"测试, 地城",2660,503,12,[10,9]',
        'ENCOUNTER_START,2926,"首领",8,5,2660',
        'SPELL_DAMAGE,Creature-1,"敌人",0,0,Player-1,"队员",0,0,20,"冲击",4',
        'SPELL_INTERRUPT,Player-1,"队员",0,0,Creature-1,"敌人",0,0,12,"打断",1,13,"读条",4',
        'SPELL_HEAL,Player-2,"治疗",0,0,Player-1,"队员",0,0,10,"回复",4',
        'UNIT_DIED,0,nil,0,0,Player-1,"队员",0,0',
        'ENCOUNTER_END,2926,"首领",8,5,0',
        'ENCOUNTER_START,2926,"首领",8,5,2660',
        'ENCOUNTER_END,2926,"首领",8,5,1',
        "CHALLENGE_MODE_END,2660,1,12,1890000,180",
    ]
    return b"\r\n".join(f"9/30/2026 18:49:{i:02}.000-4  {row}".encode() for i, row in enumerate(records))


class CombatLogTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(username="日志测试")
        self.client.force_login(self.user)
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(COMBAT_LOG_ROOT=Path(root)))
        self.enterContext(patch.object(archive, "PART_SIZE", 192))
        self.data = sample()
        response = self.client.post(reverse("combat_logs"), {"name": "测试.txt", "size": len(self.data)})
        self.url = response.json()["url"]
        self.report = CombatLogReport.objects.get()
        self.parts = [self.data[i:i+192] for i in range(0, len(self.data), 192)]

    def put(self, i, chunk):
        return self.client.put(f"{self.url}parts/{i}/", chunk, content_type="application/octet-stream")

    def finish(self):
        for i in reversed(range(len(self.parts))):
            self.assertEqual(self.put(i, self.parts[i]).status_code, 200)
        with patch.object(archive, "launch", side_effect=archive.collect) as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.post(self.url + "finish/").status_code, 200)
            self.client.post(self.url + "finish/")
            self.assertEqual(dispatch.call_count, 1)
        self.report.refresh_from_db()

    def test_resume_integrity_and_release(self):
        self.assertEqual(self.client.post(self.url + "finish/").status_code, 400)
        self.assertEqual(self.put(1, self.parts[1]).status_code, 200)
        self.assertEqual(self.put(1, b"x"*192).status_code, 400)
        self.assertEqual(self.client.get(self.url + "parts/").json()["parts"],
                         {"1": hashlib.sha256(self.parts[1]).hexdigest()})
        self.finish()
        self.assertEqual(self.report.digest, hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.report.state, "ready")
        self.assertEqual(self.report.summary["orphan_keys"], 1)
        self.assertEqual(b"".join(self.client.get(self.url + "original/").streaming_content), self.data)
        view = self.client.get(self.url + "?fight=2&death=8")
        self.assertEqual([f["result"] for f in view.context["fights"]], ["已完成", "失败", "击杀"])
        self.assertEqual([f["deaths"] for f in view.context["fights"]], [1, 1, 0])
        self.assertEqual([e["line"] for e in view.context["evidence"]], [5, 7, 8])
        for row in view.context["evidence"]:
            self.assertEqual(row["raw"], self.data.splitlines()[row["line"]-1].decode())
        self.assertContains(view, "读条")
        self.assertEqual(self.client.get(self.url + "?fight=3&death=8").status_code, 404)

    def test_other_accounts_and_permissions(self):
        self.client.force_login(get_user_model().objects.create_superuser(username="另一用户"))
        for suffix in ("", "parts/", "original/"):
            self.assertEqual(self.client.get(self.url + suffix).status_code, 404)
        self.assertEqual(self.put(0, self.parts[0]).status_code, 404)
        self.assertEqual(self.client.post(self.url + "finish/").status_code, 404)
        self.client.force_login(get_user_model().objects.create_user(username="普通成员"))
        entry = 'href="' + reverse("combat_logs") + '"'
        self.assertContains(self.client.get(reverse("portal_home")), entry)
        library = self.client.get(reverse("combat_logs"))
        self.assertTemplateUsed(library, "portal/_header.html")
        self.assertNotContains(library, self.report.filename)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.put(0, self.parts[0]).status_code, 404)
        self.client.logout()
        self.assertNotContains(self.client.get(reverse("portal_home")), entry)
        for path in (reverse("combat_logs"), self.url + "analysis/?fight=1", self.url + "replay/1/", self.url + "original/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            redirect = urlsplit(response.url)
            self.assertEqual(redirect.path, reverse("login"))
            self.assertEqual(parse_qs(redirect.query)["next"], [path])

    def test_ai_only_accepts_existing_evidence(self):
        self.finish()
        with patch("core.glm.GLMClient") as constructor:
            client = constructor.return_value
            client.available = True
            client.send_message.return_value = "[5, 7]"
            url = self.url + "highlights/"
            args = {"fight": 2, "death": 8}
            self.assertEqual(self.client.post(url, args).json()["lines"], [5, 7])
            prompt = client.send_message.call_args.args[0]
            for private in ["Player-", "队员", "首领", "冲击"]:
                self.assertNotIn(private, prompt)
            client.send_message.return_value = "[100]"
            self.assertEqual(self.client.post(url, args).status_code, 502)

    def test_byte_splits_preserve_offsets(self):
        lines = list(iter_lines(bytes([b]) for b in self.data))
        self.assertEqual(b"".join(row[2] for row in lines), self.data)
        for offset, length, raw in lines:
            self.assertEqual(raw, self.data[offset:offset+length])

    def test_cutoff_keeps_completed_and_partial_activities(self):
        import sqlite3
        from contextlib import closing

        path = archive.directory(self.report) / "partial.sqlite3"
        build_index(path, self.data.splitlines(keepends=True)[:10])
        with closing(sqlite3.connect(path)) as db:
            rows = db.execute("SELECT result,end_ms FROM fight ORDER BY id").fetchall()
        self.assertEqual([row[0] for row in rows], ["终点缺失", "失败", "终点缺失"])
        self.assertIsNone(rows[2][1])

    def test_invalid_text_can_retry_with_new_attempt(self):
        self.parts = [b"x"*len(chunk) for chunk in self.parts]
        with self.assertLogs(archive.logger, level="ERROR"):
            for i, chunk in enumerate(self.parts):
                self.put(i, chunk)
            with patch.object(archive, "launch", side_effect=archive.collect):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post(self.url + "finish/")
                self.report.refresh_from_db()
                self.assertEqual(self.report.state, "failed")
                previous = self.report.attempt
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post(self.url + "finish/")
        self.report.refresh_from_db()
        self.assertNotEqual(previous, self.report.attempt)
        self.assertContains(self.client.get(self.url), "未找到正式服")
