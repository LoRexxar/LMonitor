"""Real ORM contracts for on-demand SimC resource reads."""
import hashlib
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.guide_models import ClassGuide
from botend.models import SimcProfile, SimcTalentString
from botend.services import simc_talent_titles as titles


class SimcResourceOnDemandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='on-demand')
        self.other = User.objects.create_user(username='other-on-demand')
        self.client.force_login(self.user)

    def talent(self, name, **kwargs):
        return SimcTalentString.objects.create(
            name=name, spec=kwargs.pop('spec', 'mage_fire'),
            talent=kwargs.pop('talent', 'UNCHANGED_CODE'),
            owner_user_id=kwargs.pop('owner_user_id', self.user.pk), **kwargs,
        )

    def test_pages_are_ordered_permission_filtered_and_identity_preserving(self):
        expected = [self.talent('same') for _ in range(28)]
        expected.insert(0, self.talent('aaa', is_system=True, owner_user_id=None))
        self.talent('hidden', owner_user_id=self.other.pk)
        inactive = self.talent('inactive', is_active=False)
        self.talent('other spec', spec='mage_frost')
        first = self.client.get('/api/simc-talent-string/', {'spec': 'mage_fire'}).json()
        self.assertEqual(first['pagination'], {'page': 1, 'page_size': 25, 'total': 29, 'total_pages': 2})
        second = self.client.get('/api/simc-talent-string/', {'spec': 'mage_fire', 'page': 2}).json()
        rows = first['data'] + second['data']
        self.assertEqual([r['id'] for r in rows], [r.pk for r in expected])
        self.assertEqual(len({r['id'] for r in rows}), 29)
        for row, original in zip(rows, expected):
            self.assertEqual((row['name'], row['talent']), (original.name, original.talent))
            self.assertTrue({'display_name', 'canonical_spec', 'hero_talent_names', 'can_edit', 'default_apl_id', 'talent_simulator_url'} <= row.keys())
        detail = self.client.get(f'/api/simc-talent-string/{expected[1].pk}/', {'page': 'bad'}).json()
        self.assertEqual(detail, {'success': True, 'data': first['data'][1]})
        self.assertEqual(self.client.get(f'/api/simc-talent-string/{inactive.pk}/').status_code, 404)
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        admin = self.client.get('/api/simc-talent-string/', {'spec': 'mage_fire', 'page_size': 100}).json()
        self.assertEqual(admin['pagination']['total'], 30)
        self.assertIn(inactive.pk, [r['id'] for r in admin['data']])

    def test_bounds_empty_pages_and_stable_cross_spec_order(self):
        self.talent('z', spec='warrior_fury')
        self.talent('z')
        self.talent('a')
        rows = self.client.get('/api/simc-talent-string/').json()['data']
        self.assertEqual([(r['spec'], r['name']) for r in rows], [('mage_fire', 'a'), ('mage_fire', 'z'), ('warrior_fury', 'z')])
        for query in ({'page': 'x'}, {'page': 0}, {'page': -1}, {'page_size': 0}, {'page_size': -1}, {'page_size': '1.5'}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/simc-talent-string/', query).status_code, 400)
        self.assertEqual(self.client.get('/api/simc-talent-string/', {'page_size': 101}).json()['pagination']['page_size'], 100)
        for query, total in (({'page': 999}, 3), ({'spec': 'no-match'}, 0)):
            result = self.client.get('/api/simc-talent-string/', query).json()
            self.assertEqual(result['data'], [])
            self.assertEqual(result['pagination']['total'], total)
            self.assertEqual(result['pagination']['total_pages'], 1 if total else 0)

    def test_only_current_page_guide_is_queried_and_parsed(self):
        guides = []
        for index in range(2):
            code = f'PAGE_CODE_{index}'
            guide = ClassGuide.objects.create(
                slug=f'on-demand-{index}', title='Fire', class_name='Mage', spec_name='Fire',
                spec_id=63, game_version='12.1.5',
                source_url='https://www.icy-veins.com/wow/fire-mage-pve-dps-guide',
                source_markdown=f'## Page {index}\n\nNo talent block needed',
            )
            guides.append(guide)
            self.talent(str(index), talent=code, is_system=True,
                        system_key=f'guide:icy-veins:{guide.pk}:mage_fire:{hashlib.sha256(code.encode()).hexdigest()}')
        titles._GUIDE_CACHE.clear()
        with patch.object(titles, '_load_guides', wraps=titles._load_guides) as load, \
             patch.object(titles, '_occurrences', wraps=titles._occurrences) as parse, \
             CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/simc-talent-string/', {'page_size': 1})
        self.assertEqual(len(response.json()['data']), 1)
        self.assertEqual(set(load.call_args.args[0]), {guides[0].pk})
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(parse.call_args.args[0], guides[0].source_markdown)
        guide_queries = [q['sql'] for q in queries if 'FROM "botend_classguide"' in q['sql']]
        self.assertTrue(guide_queries)
        for sql in guide_queries:
            self.assertIn(f'IN ({guides[0].pk})', sql)
        resource_reads = [q['sql'] for q in queries if f'FROM "{SimcTalentString._meta.db_table}"' in q['sql'] and 'COUNT(' not in q['sql']]
        self.assertEqual(len(resource_reads), 1)
        self.assertIn('LIMIT 1', resource_reads[0])

    def test_profile_canonical_filter_keeps_legacy_shapes_permissions_and_unfiltered_contract(self):
        selected = []
        for spec, cls in [('mage_frost', 'mage'), ('frost', 'mage'), ('frost', 'mage_frost'), ('', 'mage_frost')]:
            selected.append(SimcProfile.objects.create(user_id=self.user.pk, name='frost', spec=spec, class_name=cls))
        upstream = SimcProfile.objects.create(name='system frost', spec='frost', class_name='mage', source=SimcProfile.SOURCE_SIMC_UPSTREAM)
        selected.append(upstream)
        fire = SimcProfile.objects.create(user_id=self.user.pk, name='fire', spec='fire', class_name='mage')
        SimcProfile.objects.create(user_id=self.user.pk, name='dk', spec='frost', class_name='deathknight')
        SimcProfile.objects.create(user_id=self.other.pk, name='private frost', spec='frost', class_name='mage')
        SimcProfile.objects.create(user_id=self.user.pk, name='inactive', spec='frost', class_name='mage', is_active=False)
        response = self.client.get('/api/simc-profile/', {'spec': 'mage_frost'}).json()
        self.assertEqual({r['id'] for r in response['data']}, {p.pk for p in selected})
        self.assertEqual({r['canonical_spec'] for r in response['data']}, {'mage_frost'})
        all_rows = self.client.get('/api/simc-profile/').json()
        self.assertIn(fire.pk, [r['id'] for r in all_rows['data']])
        self.assertNotIn('pagination', all_rows)
        self.assertIn('talent_versions', all_rows)
        self.assertEqual(self.client.get('/api/simc-profile/', {'spec': 'invalid'}).status_code, 400)
