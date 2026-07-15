import io

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from botend.models import SimcContentTemplate, SimcProfile, SimcTask, SimcTaskBatch, UserAplStorage


class CleanupLegacySimcTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cleanup-user')
        self.profile = SimcProfile.objects.create(user_id=self.user.id, name='protected', spec='fury')
        self.apl = UserAplStorage.objects.create(user_id=self.user.id, title='protected', apl_code='actions=/wait')
        self.template = SimcContentTemplate.objects.create(
            owner_user_id=self.user.id, name='protected', template_type='custom_apl',
            spec='fury', content='actions=/wait')
        self.empty_batch = SimcTaskBatch.objects.create(user_id=self.user.id, name='empty')
        self.bad = SimcTask.objects.create(
            user_id=self.user.id, name='legacy', simc_profile_id=self.profile.id,
            current_status=3, final_simc_content='')
        self.good = SimcTask.objects.create(
            user_id=self.user.id, name='good', simc_profile_id=self.profile.id,
            current_status=2, final_simc_content='warrior="ok"')

    def test_dry_run_apply_and_idempotency(self):
        call_command('cleanup_legacy_simc', stdout=io.StringIO())
        self.assertTrue(SimcTask.objects.filter(id=self.bad.id).exists())
        call_command('cleanup_legacy_simc', apply=True, stdout=io.StringIO())
        self.assertFalse(SimcTask.objects.filter(id=self.bad.id).exists())
        self.assertTrue(SimcTask.objects.filter(id=self.good.id).exists())
        self.assertFalse(SimcTaskBatch.objects.filter(id=self.empty_batch.id).exists())
        self.assertTrue(SimcProfile.objects.filter(id=self.profile.id).exists())
        self.assertTrue(UserAplStorage.objects.filter(id=self.apl.id).exists())
        self.assertTrue(SimcContentTemplate.objects.filter(id=self.template.id).exists())
        call_command('cleanup_legacy_simc', apply=True, stdout=io.StringIO())
        self.assertTrue(SimcTask.objects.filter(id=self.good.id).exists())
