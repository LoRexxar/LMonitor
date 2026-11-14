"""Real ORM regression: project titles only for the selected public spec."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.guide_models import ClassGuide
from botend.models import SimcApl, SimcProfile, SimcTalentString
from botend.services import simc_talent_titles
from botend.services.simc_reference import reference_catalog
from botend.services.simc_task_service import _build_talent_payload, _compute_content_hash
from botend.tests import test_simc_talent_titles as title_fixtures
from botend.tests.simc_reference_test_fixtures import publish_resources


class PortalReferenceOnDemandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        title_fixtures.TalentDisplayTitleTests.setUpTestData.__func__(cls)
        cls.user = User.objects.create_user(username='on-demand-reference')
        SimcTalentString.objects.filter(pk__in=cls.ids).update(is_system=True)
        for talent in SimcTalentString.objects.filter(pk__in=cls.ids):
            publish_resources(talent.spec, talents=[talent])
        cls.selected = SimcTalentString.objects.get(pk=cls.ids[0])
        cls.apl = SimcApl.objects.create(
            name='Linked default', spec=cls.selected.spec, is_system=True, is_selectable=True,
        )
        cls.selected.default_apl = cls.apl
        cls.selected.save(update_fields=['default_apl'])
        cls.profile = SimcProfile.objects.create(
            name='Public profile', spec=cls.selected.spec, class_name='mage',
        )
        cls.publication = publish_resources(
            cls.selected.spec, profiles=[cls.profile], apls=[cls.apl],
        )

    def test_selected_spec_only_parses_its_guides_and_preserves_public_contract(self):
        self.client.force_login(self.user)
        url = reverse('portal_simc_reference')
        before = _build_talent_payload(self.selected)
        before_hash = _compute_content_hash(before)
        # The default catalogue remains the display-ready publication contract.
        _, expected = reference_catalog()
        _, management = reference_catalog(public_only=False)
        self.assertTrue(all('display_name' in row for spec in management.values()
                            for row in spec['talents']))
        selected_guides = {
            int(row['system_key'].split(':')[2])
            for row in expected[self.selected.spec]['talents']
        }
        self.assertGreater(ClassGuide.objects.count(), len(selected_guides))
        simc_talent_titles._GUIDE_CACHE.clear()
        try:
            with patch('requests.sessions.Session.request', side_effect=AssertionError('No network')), \
                    patch.object(simc_talent_titles, '_load_guides', wraps=simc_talent_titles._load_guides) as load, \
                    patch.object(simc_talent_titles, '_occurrences', wraps=simc_talent_titles._occurrences) as parse:
                response = self.client.get(url, {'spec': self.selected.spec, 'talent': self.selected.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([call.args[0] for call in load.call_args_list], [selected_guides])
            expected_sources = list(ClassGuide.objects.filter(pk__in=selected_guides)
                                    .values_list('source_markdown', flat=True))
            self.assertCountEqual([call.args[0] for call in parse.call_args_list], expected_sources)
            actual = response.context['selected_spec']
            self.assertEqual(actual['talents'], expected[self.selected.spec]['talents'])
            self.assertEqual(response.context['apl'].pk, self.apl.pk)
            self.assertTrue(response.context['apl_is_default'])
            self.assertEqual(response.context['profile'].pk, self.profile.pk)
            groups = {row['key']: row for group in response.context['spec_groups'] for row in group['specs']}
            self.assertEqual(set(groups), set(expected))
            for key, spec in expected.items():
                self.assertEqual(groups[key]['has_data'], spec['has_data'])
                for kind in ('profiles', 'talents', 'apls'):
                    self.assertEqual([row['id'] for row in groups[key][kind]],
                                     [row['id'] for row in spec[kind]])
            self.selected.refresh_from_db()
            self.assertEqual(_build_talent_payload(self.selected), before)
            self.assertEqual(_compute_content_hash(_build_talent_payload(self.selected)), before_hash)

            private = SimcTalentString.objects.create(
                name='Private', spec=self.selected.spec, talent='private',
                is_system=True, owner_user_id=self.user.pk,
            )
            inactive = SimcTalentString.objects.create(
                name='Inactive', spec=self.selected.spec, talent='inactive', is_system=True, is_active=False,
            )
            unpublished = SimcTalentString.objects.create(
                name='Unpublished', spec=self.selected.spec, talent='unpublished', is_system=True,
            )
            # Even publication cannot make private/inactive or wrong-spec resources public.
            foreign = SimcTalentString.objects.exclude(spec=self.selected.spec).first()
            self.publication.talents.add(private, inactive, foreign)
            for talent_id in ('abc', '-1', '１２', str(private.pk), str(inactive.pk),
                              str(unpublished.pk), str(foreign.pk)):
                with self.subTest(talent=talent_id):
                    self.assertEqual(self.client.get(url, {'spec': self.selected.spec, 'talent': talent_id}).status_code, 404)
            self.assertEqual(self.client.get(url, {'spec': 'invalid'}).status_code, 404)
            self.client.logout()
            with patch('botend.portal.simc_reference.reference_context') as context:
                self.assertEqual(self.client.get(url).status_code, 302)
            context.assert_not_called()
        finally:
            simc_talent_titles._GUIDE_CACHE.clear()
