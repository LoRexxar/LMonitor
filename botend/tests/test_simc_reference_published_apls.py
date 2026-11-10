"""公开清单中的 APL 是执行选择，不依赖天赋的默认 APL 关联。"""
from unittest.mock import patch

from django.test import TestCase

from botend.models import SimcApl, SimcReferenceSimulation, SimcTalentString, SimcTask
from botend.services import simc_reference_simulations as service
from botend.services.simc_reference_management import public_combination_keys
from botend.services.task_resolver import resolve_task
from botend.tests import test_simc_reference_simulations as fixtures


class PublishedAplSelectionTests(TestCase):
    setUp = fixtures.ReferenceSimulationTests.setUp
    enqueue = fixtures.ReferenceSimulationTests.enqueue
    dispatch = fixtures.ReferenceSimulationTests.dispatch
    complete = fixtures.ReferenceSimulationTests.complete
    results = fixtures.ReferenceSimulationTests.results

    def clear_default(self):
        SimcTalentString.objects.filter(pk=self.talent.pk).update(default_apl=None)
        self.talent.refresh_from_db()

    def alternate(self, *, publish=True):
        apl = SimcApl.objects.create(name='另一份公开 APL', spec='warrior_fury', class_name='warrior',
                                    is_system=True, is_selectable=True, content='actions=/bloodthirst',
                                    source='simc_upstream' if publish else 'user',
                                    owner_user_id=None if publish else self.user.pk)
        if publish:
            self.publication.apls.add(apl)
        return apl

    def test_null_default_has_real_start_form_and_posts_the_published_apl(self):
        self.clear_default()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        row = response.context['scheme_page'][0]
        self.assertTrue(row['can_run'])
        self.assertEqual(row['apl_id'], self.apl.pk)
        self.assertContains(response, '开始模拟')
        self.assertNotContains(response, '绑定已公开的默认 APL')
        response = self.client.post(self.url, {'action': 'start_scheme', 'config': self.config.pk,
            'profile': self.profile.pk, 'talent': self.talent.pk, 'apl': self.apl.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['result']['created'], 2)
        self.assertEqual(set(SimcReferenceSimulation.objects.values_list('apl_id', flat=True)), {self.apl.pk})
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertIsNone(SimcTalentString.objects.get(pk=self.talent.pk).default_apl_id)

    def test_matrix_contains_every_explicitly_published_apl_not_private_default(self):
        other = self.alternate()
        private_default = self.alternate(publish=False)
        SimcTalentString.objects.filter(pk=self.talent.pk).update(default_apl=private_default)
        self.assertEqual(set(public_combination_keys(self.config)), {
            (self.profile.pk, self.talent.pk, apl.pk) for apl in (self.apl, other)})
        self.assertEqual(self.enqueue(), {'created': 4, 'skipped': 0, 'errors': []})
        self.assertEqual(self.enqueue()['skipped'], 4)
        self.assertFalse(SimcReferenceSimulation.objects.filter(apl=private_default).exists())

    def test_one_selected_combination_is_scoped_even_if_not_talent_default(self):
        other = self.alternate()
        result = self.enqueue(combination=(self.profile.pk, self.talent.pk, other.pk))
        self.assertEqual(result, {'created': 2, 'skipped': 0, 'errors': []})
        self.assertEqual(set(SimcReferenceSimulation.objects.values_list('apl_id', flat=True)), {other.pk})

    def test_worker_creates_real_tasks_with_explicit_apl_after_default_changes(self):
        self.clear_default()
        other = self.alternate()
        self.assertEqual(self.enqueue()['created'], 4)
        private_default = self.alternate(publish=False)
        SimcTalentString.objects.filter(pk=self.talent.pk).update(default_apl=private_default)
        for _ in range(2):
            self.dispatch()
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='submitted').count(), 4)
        self.assertEqual(SimcTask.objects.count(), 4)
        for row in SimcReferenceSimulation.objects.select_related('task', 'apl'):
            resolved = resolve_task(row.task)
            self.assertEqual(row.task.apl_id, row.apl_id)
            self.assertEqual(row.task.apl_version.resource_id, row.apl_id)
            self.assertEqual(resolved.apl_content, row.apl.content)
            self.assertIn(row.apl_id, {self.apl.pk, other.pk})
            self.assertEqual(row.snapshot['resources']['apl'][0], row.apl_id)
        self.assertEqual(SimcTalentString.objects.get(pk=self.talent.pk).default_apl_id, private_default.pk)

    def test_revoked_apl_during_preparation_cannot_fall_back_to_default(self):
        self.clear_default()
        self.assertEqual(self.enqueue()['created'], 2)
        prepare = service.tasks.prepare_task_creation
        def revoke(**kwargs):
            prepared = prepare(**kwargs)
            self.publication.apls.remove(self.apl)
            return prepared
        with patch.object(service.tasks, 'prepare_task_creation', side_effect=revoke):
            self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)

    def test_results_and_rankings_do_not_disappear_when_default_is_cleared(self):
        from botend.services.simc_reference_rankings import current_rankings
        self.enqueue()
        self.complete()
        before = [(row['signature'], row['dps']) for row in self.results()]
        self.clear_default()
        self.assertEqual([(row['signature'], row['dps']) for row in self.results()], before)
        self.assertEqual(len(current_rankings(self.config)), 2)
        self.publication.apls.remove(self.apl)
        self.assertEqual(self.results(), [])
        self.assertEqual(current_rankings(self.config), {})

    def test_missing_published_apl_is_still_blocked_with_correct_message(self):
        self.publication.apls.clear()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertFalse(response.context['scheme_page'][0]['can_run'])
        self.assertContains(response, '请在公开配置管理中选择 APL')
        self.assertNotContains(response, '开始模拟')
        self.assertEqual(self.enqueue()['created'], 0)
        self.assertEqual(SimcTask.objects.count(), 0)
