"""
SimC Model Contract Tests

Tests for SimcContentTemplate active_unique_key logic and the request/run model:
- Global templates: one active per (type, spec)
- Legacy owner-scoped rows still obey the active uniqueness key
- Different users don't conflict
- Inactive templates can duplicate
- One request is represented by one task (there is no batch model/FK)
- Candidate identity and parameters live on SimulationRun

Tests for SimcApl uniqueness and naming:
- Global system APLs: one active per spec
- User APLs: multiple different names allowed per (owner, spec)
- Same normalized name rejected within (owner, spec)
"""
from django.test import TestCase
from django.db import IntegrityError, transaction
from botend.models import SimcApl, SimcBackendBinary, SimcContentTemplate, SimcTask, SimulationRun


class SimcContentTemplateGlobalUniqueTests(TestCase):
    """Test global template uniqueness constraints."""

    def test_global_base_template_second_active_same_spec_rejected(self):
        """Second active global base_template with same spec must fail."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='first',
            is_active=True,
            owner_user_id=None,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcContentTemplate.objects.create(
                    template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
                    spec='warrior_fury',
                    content='second',
                    is_active=True,
                    owner_user_id=None,
                )

    def test_global_default_player_second_active_same_spec_rejected(self):
        """Second active global default_player with same spec must fail."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            spec='warrior_fury',
            content='first',
            is_active=True,
            owner_user_id=None,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcContentTemplate.objects.create(
                    template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
                    spec='warrior_fury',
                    content='second',
                    is_active=True,
                    owner_user_id=None,
                )


class SimcContentTemplateInactiveAllowsDuplicateTests(TestCase):
    """Test inactive templates can duplicate."""

    def test_inactive_templates_can_duplicate_same_spec(self):
        """Multiple inactive templates with same type+spec are allowed."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='first',
            is_active=False,
            owner_user_id=None,
        )

        # Should succeed
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='second',
            is_active=False,
            owner_user_id=None,
        )

        # Verify both exist
        self.assertEqual(
            SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
                spec='warrior_fury',
                is_active=False,
            ).count(),
            2
        )


class SimcContentTemplateUserIsolationTests(TestCase):
    """Legacy owner-scoped base rows retain their database uniqueness behavior."""

    def test_same_user_duplicate_base_template_rejected(self):
        """Same user cannot have two active base_template for same spec."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='first',
            is_active=True,
            owner_user_id=1001,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcContentTemplate.objects.create(
                    template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
                    spec='warrior_fury',
                    content='second',
                    is_active=True,
                    owner_user_id=1001,
                )


class SimcAplNamingTests(TestCase):
    """Test SimcApl naming uniqueness rules."""

    def test_same_user_custom_apl_different_names_same_spec_allowed(self):
        """Same user can have multiple custom APLs with different names for same spec."""
        SimcApl.objects.create(
            name='Single Target',
            spec='warrior_fury',
            content='actions+=/bloodthirst',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1001,
            is_active=True,
        )

        # Should succeed with different name
        SimcApl.objects.create(
            name='AoE Build',
            spec='warrior_fury',
            content='actions+=/whirlwind',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1001,
            is_active=True,
        )

        # Verify both exist
        self.assertEqual(
            SimcApl.objects.filter(
                spec='warrior_fury',
                owner_user_id=1001,
                is_active=True,
            ).count(),
            2
        )

    def test_same_user_custom_apl_normalized_same_name_rejected(self):
        """Same user cannot have custom APL with same normalized name (case/whitespace)."""
        SimcApl.objects.create(
            name='Single Target',
            spec='warrior_fury',
            content='first',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1001,
            is_active=True,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcApl.objects.create(
                    name='single target',  # Same normalized name
                    spec='warrior_fury',
                    content='second',
                    source=SimcApl.SOURCE_USER,
                    owner_user_id=1001,
                    is_active=True,
                )

    def test_same_user_custom_apl_whitespace_variation_rejected(self):
        """Same user cannot have custom APL with whitespace-only name difference."""
        SimcApl.objects.create(
            name='MyBuild',
            spec='warrior_fury',
            content='first',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1001,
            is_active=True,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcApl.objects.create(
                    name=' MyBuild ',  # Same after stripping
                    spec='warrior_fury',
                    content='second',
                    source=SimcApl.SOURCE_USER,
                    owner_user_id=1001,
                    is_active=True,
                )

    def test_different_users_custom_apl_same_name_allowed(self):
        """Different users can have custom APL with same name for same spec."""
        SimcApl.objects.create(
            name='Raid Build',
            spec='warrior_fury',
            content='user1 content',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1001,
            is_active=True,
        )

        # Should succeed
        SimcApl.objects.create(
            name='Raid Build',
            spec='warrior_fury',
            content='user2 content',
            source=SimcApl.SOURCE_USER,
            owner_user_id=1002,
            is_active=True,
        )

        # Verify both exist
        self.assertEqual(
            SimcApl.objects.filter(
                spec='warrior_fury',
                name='Raid Build',
                is_active=True,
            ).count(),
            2
        )


class SimcContentTemplateActiveUniqueKeyRecalculationTests(TestCase):
    """Test active_unique_key is recalculated on save."""

    def test_save_recalculates_active_unique_key(self):
        """Saving a template always recalculates active_unique_key."""
        tpl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='content',
            is_active=True,
            owner_user_id=None,
        )

        original_key = tpl.active_unique_key
        self.assertIsNotNone(original_key)

        # Modify and save
        tpl.content = 'updated content'
        tpl.save()

        # Key should remain the same (same type+spec+owner+active)
        self.assertEqual(tpl.active_unique_key, original_key)

    def test_deactivating_template_sets_key_to_null(self):
        """Deactivating a template sets active_unique_key to NULL."""
        tpl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='content',
            is_active=True,
            owner_user_id=None,
        )

        self.assertIsNotNone(tpl.active_unique_key)

        # Deactivate
        tpl.is_active = False
        tpl.save()

        self.assertIsNone(tpl.active_unique_key)

    def test_reactivating_template_recalculates_key(self):
        """Reactivating a template recalculates active_unique_key."""
        tpl = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='warrior_fury',
            content='content',
            is_active=False,
            owner_user_id=None,
        )

        self.assertIsNone(tpl.active_unique_key)

        # Activate
        tpl.is_active = True
        tpl.save()

        self.assertIsNotNone(tpl.active_unique_key)
        self.assertEqual(tpl.active_unique_key, 'base_template:global:warrior_fury')


class SimcRequestRunModelContractTests(TestCase):
    def setUp(self):
        self.backend, _ = SimcBackendBinary.objects.get_or_create(
            identifier='production',
            defaults={'name': '正式服', 'platform': 'linux64'},
        )

    def test_task_has_analysis_result_but_no_batch_field(self):
        field_names = {field.name for field in SimcTask._meta.get_fields()}
        self.assertIn('analysis_result', field_names)
        self.assertNotIn('batch', field_names)

        task = SimcTask.objects.create(
            user_id=1001, name='Request', simc_profile_id=1, backend=self.backend,
        )
        self.assertEqual(task.analysis_result, {})

    def test_run_stores_candidate_contract(self):
        task = SimcTask.objects.create(
            user_id=1001, name='Request', simc_profile_id=1, backend=self.backend,
        )
        run = SimulationRun.objects.create(
            task=task,
            sequence=1,
            candidate_key='baseline',
            candidate_label='Baseline',
            round_number=0,
            candidate_params={'is_base': True, 'stats': {}},
        )
        run.refresh_from_db()
        self.assertEqual(run.candidate_key, 'baseline')
        self.assertEqual(run.round_number, 0)
        self.assertEqual(run.candidate_params['is_base'], True)
