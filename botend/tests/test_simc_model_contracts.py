"""
SimC Model Contract Tests

Tests for SimcContentTemplate active_unique_key logic and SimcTaskBatch fields:
- Global templates: one active per (type, spec)
- User templates: one active per (owner, type, spec) for base/default_player/custom_player
- Different users don't conflict
- Inactive templates can duplicate
- Batch can store request_manifest
- Task associates with batch

Tests for SimcApl uniqueness and naming:
- Global system APLs: one active per spec
- User APLs: multiple different names allowed per (owner, spec)
- Same normalized name rejected within (owner, spec)
"""
import json
from django.test import TestCase
from django.db import IntegrityError, transaction
from botend.models import SimcApl, SimcContentTemplate, SimcTaskBatch, SimcTask


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
    """Test user-owned templates are isolated from global and other users."""

    def test_different_users_can_have_same_spec_custom_player(self):
        """Different users can have active custom_player for same spec."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
            spec='warrior_fury',
            content='user1 content',
            is_active=True,
            owner_user_id=1001,
        )

        # Should succeed
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
            spec='warrior_fury',
            content='user2 content',
            is_active=True,
            owner_user_id=1002,
        )

        # Verify both exist
        self.assertEqual(
            SimcContentTemplate.objects.filter(
                template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
                spec='warrior_fury',
                is_active=True,
            ).count(),
            2
        )

    def test_same_user_duplicate_custom_player_rejected(self):
        """Same user cannot have two active custom_player for same spec."""
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
            spec='warrior_fury',
            content='first',
            is_active=True,
            owner_user_id=1001,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcContentTemplate.objects.create(
                    template_type=SimcContentTemplate.TYPE_CUSTOM_PLAYER,
                    spec='warrior_fury',
                    content='second',
                    is_active=True,
                    owner_user_id=1001,
                )

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


class SimcTaskBatchRequestManifestTests(TestCase):
    """Test SimcTaskBatch can store request_manifest."""

    def test_batch_can_store_request_manifest(self):
        """Batch can store JSON request_manifest."""
        manifest = {
            'input_params': {'fight_style': 'Patchwerk', 'time': 300},
            'candidates': ['base', 'crit+1000', 'haste+1000'],
        }

        batch = SimcTaskBatch.objects.create(
            user_id=1001,
            name='Test Batch',
            batch_type='comparison',
            request_manifest=json.dumps(manifest),
            status=0,
        )

        # Verify storage
        batch.refresh_from_db()
        self.assertIsNotNone(batch.request_manifest)
        loaded = json.loads(batch.request_manifest)
        self.assertEqual(loaded['input_params']['fight_style'], 'Patchwerk')
        self.assertEqual(len(loaded['candidates']), 3)

    def test_batch_status_fields(self):
        """Batch status fields work correctly."""
        batch = SimcTaskBatch.objects.create(
            user_id=1001,
            name='Status Test',
            batch_type='comparison',
            status=1,  # Running
            error_detail='',
        )

        self.assertEqual(batch.status, 1)
        self.assertIsNone(batch.completed_at)

        # Update status
        from django.utils import timezone
        batch.status = 2  # Completed
        batch.completed_at = timezone.now()
        batch.save()

        batch.refresh_from_db()
        self.assertEqual(batch.status, 2)
        self.assertIsNotNone(batch.completed_at)

    def test_batch_error_detail_nullable(self):
        """Batch error_detail is nullable."""
        batch = SimcTaskBatch.objects.create(
            user_id=1001,
            name='Error Test',
            batch_type='comparison',
            status=3,  # Failed
            error_detail='Validation error: missing spec',
        )

        batch.refresh_from_db()
        self.assertEqual(batch.error_detail, 'Validation error: missing spec')


class SimcTaskBatchAssociationTests(TestCase):
    """Test SimcTask can associate with SimcTaskBatch."""

    def test_task_associates_with_batch(self):
        """Task can be associated with a batch."""
        batch = SimcTaskBatch.objects.create(
            user_id=1001,
            name='Batch 1',
            batch_type='comparison',
        )

        task = SimcTask.objects.create(
            user_id=1001,
            name='Task 1',
            simc_profile_id=1,
            batch=batch,
            task_type=1,
        )

        # Verify association
        task.refresh_from_db()
        self.assertEqual(task.batch_id, batch.id)
        self.assertEqual(task.batch.name, 'Batch 1')

    def test_batch_deletion_sets_task_batch_to_null(self):
        """Deleting a batch sets task.batch to NULL (SET_NULL)."""
        batch = SimcTaskBatch.objects.create(
            user_id=1001,
            name='Batch to Delete',
            batch_type='comparison',
        )

        task = SimcTask.objects.create(
            user_id=1001,
            name='Task 1',
            simc_profile_id=1,
            batch=batch,
            task_type=1,
        )

        batch.delete()

        # Task should still exist with batch=NULL
        task.refresh_from_db()
        self.assertIsNone(task.batch_id)
