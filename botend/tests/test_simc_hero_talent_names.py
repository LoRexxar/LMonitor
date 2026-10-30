"""Hero names follow selected physical subtrees, not incomplete anchor rows."""
from django.test import TestCase

from botend.dashboard.api import _resolve_simc_talent_hero_names
from botend.models import WowTalentNodeMetadata, WowTalentVersion
from botend.services.simc_hero_talents import HeroTalentAnalysisError, resolve_hero_talent_names
from botend.services.spec_stats_service import _hero_subtree_display_title, _hero_subtree_name_from_table
from botend.wow.talents.build_code import TalentBuildCodeEncoder, _ImportBitWriter
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.service import TalentBuildCodeService


class SimcHeroTalentNameTests(TestCase):
    def setUp(self):
        self.retail, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'branch': 'retail', 'is_active': True, 'status': 'active',
            'source_dir': '', 'current_build': '12.1.0.hero-test',
        })
        self.ptr, _ = WowTalentVersion.objects.update_or_create(key='ptr', defaults={
            'branch': 'ptr', 'is_active': True, 'status': 'active',
            'source_dir': '', 'current_build': '12.1.0.hero-test-ptr',
        })

    def node(self, entry, subtree, *, version=None, tree='hero', spec='Fury', name='Hero skill'):
        return WowTalentNodeMetadata.objects.create(
            talent_version=version or self.retail, class_name='Warrior', spec_name=spec,
            tree_type=tree, node_id=entry, talent_id=entry, spell_id=entry,
            db2_subtree_id=subtree, row=1, column=entry, max_points=1,
            name=name, source='db2_backfill',
        )

    def code(self, *selected, branch='retail'):
        # Real encoder/decoder/render pipeline over a deliberately small DB2 fixture.
        header = _ImportBitWriter()
        header.write(2, 8)
        header.write(72, 16)  # Fury
        for _ in range(16):
            header.write(0, 8)
        reference = header.to_string()
        nodes = TalentMetadataProvider(version_key=branch).get_decoder_node_list('Warrior')
        code = TalentBuildCodeEncoder.encode_node_states(reference, nodes, [
            {'tree_type': 'hero', 'node_id': entry, 'talent_id': entry,
             'points': 1, 'selected': True, 'purchased': True}
            for entry in selected
        ])
        self.assertTrue(code)
        return code

    def test_selected_hero_without_usable_anchor_resolves_canonical_name(self):
        self.node(990101, 60)
        self.node(990102, 60)
        self.node(990103, 61)
        self.node(990104, 0, tree='hero_anchor', name='Slayer')
        code = self.code(990101, 990102)
        payload = TalentBuildCodeService.build_api_view(
            talent_build_code=code, class_name='Warrior', spec_name='Fury', version_key='retail')
        self.assertEqual(payload['talent_parse_status'], 'success')
        self.assertEqual(resolve_hero_talent_names(code, 'warrior_fury'), ['屠戮者'])
        self.assertEqual(_resolve_simc_talent_hero_names('warrior_fury', code), ['屠戮者'])
        self.assertEqual(_hero_subtree_display_title('Warrior', 'Fury', 60), '屠戮者')
        # Canonical names also win over stale/noncanonical anchor labels.
        self.node(990105, 60, tree='hero_anchor', name='Stale anchor label')
        self.assertEqual(resolve_hero_talent_names(self.code(990101), 'warrior_fury'), ['屠戮者'])

    def test_unknown_selected_subtree_reports_missing_name_not_missing_selection(self):
        self.node(990111, 9999)
        with self.assertRaisesRegex(HeroTalentAnalysisError, '名称缺失.*9999'):
            resolve_hero_talent_names(self.code(990111), 'warrior_fury')
        with self.assertRaisesRegex(HeroTalentAnalysisError, '未解析到已选择'):
            resolve_hero_talent_names(self.code(), 'warrior_fury')

    def test_known_foreign_subtree_cannot_be_named_by_an_anchor(self):
        self.node(990121, 24)
        self.node(990122, 24, tree='hero_anchor', name="Elune's Chosen")
        self.assertEqual(_hero_subtree_display_title('Warrior', 'Fury', 24), '英雄天赋')
        with self.assertRaisesRegex(HeroTalentAnalysisError, '名称缺失.*24'):
            resolve_hero_talent_names(self.code(990121), 'warrior_fury')
        with self.assertRaisesRegex(HeroTalentAnalysisError, '名称缺失.*24'):
            _resolve_simc_talent_hero_names('warrior_fury', self.code(990121))

    def test_unknown_subtree_fallback_is_branch_and_spec_scoped_and_unambiguous(self):
        self.node(990131, 9999)
        self.node(990131, 9999, version=self.ptr)
        self.node(990132, 9999, version=self.ptr, tree='hero_anchor', name='PTR verified hero')
        self.node(990133, 9999, tree='hero_anchor', spec='Arms', name='Other spec hero')
        self.node(990134, 9999, tree='hero_anchor', name='[DNT] Placeholder')
        with self.assertRaisesRegex(HeroTalentAnalysisError, '名称缺失'):
            resolve_hero_talent_names(self.code(990131), 'warrior_fury')
        self.assertEqual(resolve_hero_talent_names(
            self.code(990131, branch='ptr'), 'warrior_fury', use_ptr=True), ['PTR verified hero'])
        self.node(990135, 9999, tree='hero_anchor', name='Retail verified hero')
        self.assertEqual(resolve_hero_talent_names(self.code(990131), 'warrior_fury'), ['Retail verified hero'])
        self.node(990136, 9999, tree='hero_anchor', name='Conflicting hero')
        self.assertEqual(_hero_subtree_name_from_table(
            9999, branch='retail', class_name='Warrior', spec_name='Fury'), '')
        with self.assertRaisesRegex(HeroTalentAnalysisError, '名称缺失'):
            resolve_hero_talent_names(self.code(990131), 'warrior_fury')
