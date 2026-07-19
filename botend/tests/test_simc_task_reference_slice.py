"""
TDD tests for SimC Task Reference-based workflow with Immutable Versions.

Architecture:
- SimcResourceVersion: immutable snapshot (resource_type, resource_id, content_hash, payload)
- Task: dual references (live FK + version FK)
- simc_task_service: create task with version snapshot
- task_resolver: read version payload at execution time
- task_rerun: copy version FK by default, generate new version on override

Run with: DJANGO_SETTINGS_MODULE=LMonitor.settings_test_sqlite python manage.py test botend.tests.test_simc_task_reference_slice
"""
import json
from django.test import TestCase
from django.utils import timezone
from botend.models import SimcTask, SimcProfile, SimcApl, SimcContentTemplate


class SimcResourceVersionModelTests(TestCase):
    """Test SimcResourceVersion model for immutable snapshots."""

    def test_resource_version_model_exists(self):
        """RED: SimcResourceVersion model should exist."""
        from botend.models import SimcResourceVersion
        self.assertTrue(hasattr(SimcResourceVersion, '_meta'))

    def test_resource_version_has_required_fields(self):
        """RED: SimcResourceVersion should have resource_type, resource_id, content_hash, payload, created_at."""
        from botend.models import SimcResourceVersion
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='abc123def456',
            payload={'content': 'warrior="Test"\nlevel=80'},
        )
        self.assertEqual(version.resource_type, 'profile')
        self.assertEqual(version.resource_id, 123)
        self.assertEqual(version.content_hash, 'abc123def456')
        self.assertIsNotNone(version.payload)
        self.assertIsNotNone(version.created_at)

    def test_resource_version_unique_constraint(self):
        """RED: resource_type + resource_id + content_hash should be unique."""
        from botend.models import SimcResourceVersion
        from django.db import IntegrityError

        SimcResourceVersion.objects.create(
            resource_type='apl',
            resource_id=1,
            content_hash='hash1',
            payload={'content': 'actions=/auto'},
        )

        # Duplicate should fail
        with self.assertRaises(IntegrityError):
            SimcResourceVersion.objects.create(
                resource_type='apl',
                resource_id=1,
                content_hash='hash1',
                payload={'content': 'different content'},
            )


class SimcTaskVersionFieldsTests(TestCase):
    """Test Task has version FK fields."""

    def setUp(self):
        self.user_id = 1001
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name="Profile",
            spec="warrior_fury",
            is_active=True,
        )

    def test_task_has_version_fk_fields(self):
        """RED: Task should have profile_version_id, template_version_id, apl_version_id."""
        from botend.models import SimcResourceVersion

        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=self.profile.id,
            content_hash='hash123',
            payload={'name': self.profile.name, 'spec': self.profile.spec},
        )

        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Task",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            profile_version_id=profile_version.id,
        )

        self.assertEqual(task.profile_id, self.profile.id)
        self.assertEqual(task.profile_version_id, profile_version.id)


class SimcTaskServiceTests(TestCase):
    """Test simc_task_service creates tasks with version snapshots."""

    def setUp(self):
        self.user_id = 1001
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name="Profile",
            spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment="warrior=\"Test\"\nlevel=80",
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name="Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=1000\ntarget_error=0.1",
            is_active=True,
        )
        self.apl = SimcApl.objects.create(
            name="APL",
            spec="warrior_fury",
            content="actions=/auto_attack",
            is_active=True,
            owner_user_id=self.user_id,
        )

    def test_task_service_validates_ownership(self):
        """RED: create_task should validate owner/system permissions."""
        from botend.services.simc_task_service import create_task, TaskCreationError

        other_profile = SimcProfile.objects.create(
            user_id=9999,
            name="Other Profile",
            spec="warrior_fury",
            is_active=True,
        )

        with self.assertRaises(TaskCreationError) as ctx:
            create_task(
                user_id=self.user_id,
                name="Task",
                profile_id=other_profile.id,
                template_id=self.template.id,
                apl_id=self.apl.id,
            )
        self.assertIn("belongs to user", str(ctx.exception))

    def test_task_service_validates_active_and_selectable(self):
        """RED: create_task should require is_active=True and is_selectable=True."""
        from botend.services.simc_task_service import create_task, TaskCreationError

        inactive_apl = SimcApl.objects.create(
            name="Inactive",
            spec="warrior_fury",
            content="actions=/noop",
            is_active=False,
            is_selectable=True,
            owner_user_id=self.user_id,
        )

        with self.assertRaises(TaskCreationError) as ctx:
            create_task(
                user_id=self.user_id,
                name="Task",
                profile_id=self.profile.id,
                template_id=self.template.id,
                apl_id=inactive_apl.id,
            )
        self.assertIn("not active", str(ctx.exception).lower())

    def test_task_service_creates_immutable_versions(self):
        """RED: create_task should generate/reuse SimcResourceVersion for each resource."""
        from botend.services.simc_task_service import create_task
        from botend.models import SimcResourceVersion

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            simulation_params={'iterations': 2000},
        )

        # Task should have version FKs set
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_version_id)

        # Versions should exist and be immutable
        profile_version = SimcResourceVersion.objects.get(pk=task.profile_version_id)
        self.assertEqual(profile_version.resource_type, 'profile')
        self.assertEqual(profile_version.resource_id, self.profile.id)
        self.assertIn('player_equipment', profile_version.payload)

    def test_task_service_reuses_existing_version(self):
        """RED: create_task should reuse version if content_hash matches."""
        from botend.services.simc_task_service import create_task
        from botend.models import SimcResourceVersion

        task1 = create_task(
            user_id=self.user_id,
            name="Task1",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        version_count_before = SimcResourceVersion.objects.filter(
            resource_type='profile',
            resource_id=self.profile.id,
        ).count()

        task2 = create_task(
            user_id=self.user_id,
            name="Task2",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        version_count_after = SimcResourceVersion.objects.filter(
            resource_type='profile',
            resource_id=self.profile.id,
        ).count()

        # Same version should be reused
        self.assertEqual(task1.profile_version_id, task2.profile_version_id)
        self.assertEqual(version_count_before, version_count_after)

    def test_task_service_normalizes_simulation_params(self):
        """RED: create_task should normalize simulation_params with whitelist."""
        from botend.services.simc_task_service import create_task

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            simulation_params={
                'iterations': 1000,
                'fight_style': 'patchwerk',
                'malicious_key': 'should_be_removed',
            },
        )

        # Only whitelisted keys should remain
        self.assertIn('iterations', task.simulation_params)
        self.assertNotIn('malicious_key', task.simulation_params)

    def test_task_service_leaves_legacy_content_empty(self):
        """Reference tasks should not have frozen field attributes."""
        from botend.services.simc_task_service import create_task

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        # After field deletion, these attributes should not exist
        self.assertFalse(hasattr(task, 'final_simc_content'))
        self.assertFalse(hasattr(task, 'input_hash'))
        self.assertFalse(hasattr(task, 'fragment_manifest'))

    def test_task_service_requires_complete_references_for_normal_mode(self):
        """RED: create_task in normal mode should require profile, template, and apl."""
        from botend.services.simc_task_service import create_task, TaskCreationError

        # Missing APL
        with self.assertRaises(TaskCreationError) as ctx:
            create_task(
                user_id=self.user_id,
                name="Task",
                profile_id=self.profile.id,
                template_id=self.template.id,
                mode='normal',
            )
        self.assertIn("complete references", str(ctx.exception).lower())

        # Missing template
        with self.assertRaises(TaskCreationError) as ctx:
            create_task(
                user_id=self.user_id,
                name="Task",
                profile_id=self.profile.id,
                apl_id=self.apl.id,
                mode='normal',
            )
        self.assertIn("complete references", str(ctx.exception).lower())

        # Missing profile
        with self.assertRaises(TaskCreationError) as ctx:
            create_task(
                user_id=self.user_id,
                name="Task",
                template_id=self.template.id,
                apl_id=self.apl.id,
                mode='normal',
            )
        self.assertIn("complete references", str(ctx.exception).lower())

    def test_task_service_requires_batch_for_candidate_modes(self):
        """Comparison and attribute sweep tasks cannot exist outside a Batch."""
        from botend.services.simc_task_service import create_task, TaskCreationError

        for mode in ('comparison', 'attribute_sweep'):
            with self.subTest(mode=mode):
                with self.assertRaises(TaskCreationError) as ctx:
                    create_task(
                        user_id=self.user_id,
                        name="Task",
                        profile_id=self.profile.id,
                        template_id=self.template.id,
                        apl_id=self.apl.id,
                        mode=mode,
                    )
                self.assertIn("requires batch_id", str(ctx.exception).lower())


class TaskResolverWithVersionsTests(TestCase):
    """Test resolver reads version payload, not live content."""

    def setUp(self):
        self.user_id = 1001
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name="Profile",
            spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment="warrior=\"Original\"\nlevel=80",
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name="Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=1000",
            is_active=True,
        )
        self.apl = SimcApl.objects.create(
            name="APL",
            spec="warrior_fury",
            content="actions=/original",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.user_id,
        )

    def test_resolver_reads_version_payload_not_live_content(self):
        """RED: resolve_task should read version payload, ignoring live resource updates."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_resolver import resolve_task

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        # Modify live resource AFTER task creation
        self.profile.player_equipment = "warrior=\"MODIFIED\"\nlevel=85"
        self.profile.save()

        # Resolver should read version payload, not live
        context = resolve_task(task)
        self.assertIn("Original", context.profile_content)
        self.assertNotIn("MODIFIED", context.profile_content)

    def test_resolver_validates_version_resource_consistency(self):
        """RED: resolve_task should verify version.resource_id matches task.profile_id."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_resolver import resolve_task, TaskResolutionError
        from botend.models import SimcResourceVersion

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        # Manually corrupt version FK (point to different resource)
        other_version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=9999,
            content_hash='different',
            payload={'content': 'corrupted'},
        )
        task.profile_version_id = other_version.id
        task.save()

        with self.assertRaises(TaskResolutionError) as ctx:
            resolve_task(task)
        self.assertIn("does not match", str(ctx.exception).lower())

    def test_resolver_allows_soft_deleted_live_resource(self):
        """RED: resolve_task should succeed even if live resource is soft-deleted (is_active=False)."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_resolver import resolve_task

        task = create_task(
            user_id=self.user_id,
            name="Task",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        # Soft-delete live resource
        self.apl.is_active = False
        self.apl.save()

        # Resolver should still read version payload
        context = resolve_task(task)
        self.assertIn("/original", context.apl_content)


class TaskRerunWithVersionsTests(TestCase):
    """Test rerun copies version FK by default, generates new version on override."""

    def setUp(self):
        self.user_id = 1001
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name="Profile",
            spec="warrior_fury",
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name="Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=1000",
            is_active=True,
        )
        self.apl = SimcApl.objects.create(
            name="APL",
            spec="warrior_fury",
            content="actions=/auto",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.user_id,
        )

    def test_rerun_copies_version_fk_by_default(self):
        """RED: create_rerun should copy version FK, not regenerate."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        original.current_status = 2
        original.save(update_fields=['current_status'])
        rerun = create_rerun(original.id, user_id=self.user_id)

        # Rerun should copy the exact immutable version FKs.
        self.assertEqual(rerun.profile_version_id, original.profile_version_id)
        self.assertEqual(rerun.template_version_id, original.template_version_id)
        self.assertEqual(rerun.apl_version_id, original.apl_version_id)

    def test_rerun_allocates_a_new_task_owned_html_result_base(self):
        """A rerun must not start without a base used for immutable Run reports."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )
        original.current_status = 2
        original.save(update_fields=['current_status'])

        rerun = create_rerun(original.id, user_id=self.user_id)

        self.assertRegex(rerun.result_file, r'^[0-9a-f]{32}\.html$')
        self.assertNotEqual(rerun.result_file, original.result_file)

    def test_rerun_with_override_generates_new_version(self):
        """RED: create_rerun with override should generate new version for overridden resource."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        new_apl = SimcApl.objects.create(
            name="New APL",
            spec="warrior_fury",
            content="actions=/new_rotation",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.user_id,
        )

        original.current_status = 2
        original.save(update_fields=['current_status'])
        rerun = create_rerun(
            original.id,
            user_id=self.user_id,
            overrides={'apl_id': new_apl.id},
        )

        # Rerun should have new APL version
        self.assertNotEqual(rerun.apl_version_id, original.apl_version_id)
        self.assertEqual(rerun.apl_id, new_apl.id)

    def test_rerun_rejects_explicit_empty_resource_overrides(self):
        """An explicit null/zero override must not pair an old version with no live FK."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun, TaskRerunError

        original = create_task(
            user_id=self.user_id, name="Original", profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
        )
        original.current_status = 2
        original.save(update_fields=['current_status'])

        for field in ('profile_id', 'template_id', 'apl_id'):
            for empty_value in (None, 0):
                with self.subTest(field=field, empty_value=empty_value):
                    with self.assertRaises(TaskRerunError):
                        create_rerun(
                            original.id, user_id=self.user_id,
                            overrides={field: empty_value},
                        )
        self.assertEqual(SimcTask.objects.count(), 1)

    def test_profile_override_keeps_legacy_profile_id_in_sync(self):
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        original = create_task(
            user_id=self.user_id, name="Original", profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
        )
        replacement = SimcProfile.objects.create(
            user_id=self.user_id, name='Replacement', spec='warrior_fury', is_active=True,
        )
        original.current_status = 2
        original.save(update_fields=['current_status'])

        rerun = create_rerun(
            original.id, user_id=self.user_id,
            overrides={'profile_id': replacement.id},
        )

        self.assertEqual(rerun.profile_id, replacement.id)
        self.assertEqual(rerun.simc_profile_id, replacement.id)

    def test_rerun_with_cross_user_apl_override_raises_error(self):
        """RED: create_rerun should reject APL override belonging to different user."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun, TaskRerunError

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        other_user_apl = SimcApl.objects.create(
            name="Other User APL",
            spec="warrior_fury",
            content="actions=/other",
            is_active=True,
            is_selectable=True,
            owner_user_id=9999,
        )

        original.current_status = 2
        original.save(update_fields=['current_status'])
        with self.assertRaises(TaskRerunError) as ctx:
            create_rerun(
                original.id,
                user_id=self.user_id,
                overrides={'apl_id': other_user_apl.id},
            )
        self.assertIn("belongs to user", str(ctx.exception))

    def test_rerun_with_inactive_apl_override_raises_error(self):
        """RED: create_rerun should reject inactive APL override."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun, TaskRerunError

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        inactive_apl = SimcApl.objects.create(
            name="Inactive APL",
            spec="warrior_fury",
            content="actions=/inactive",
            is_active=False,
            owner_user_id=self.user_id,
        )

        original.current_status = 2
        original.save(update_fields=['current_status'])
        with self.assertRaises(TaskRerunError) as ctx:
            create_rerun(
                original.id,
                user_id=self.user_id,
                overrides={'apl_id': inactive_apl.id},
            )
        self.assertIn("not active", str(ctx.exception).lower())

    def test_rerun_normalizes_simulation_params(self):
        """RED: create_rerun should normalize simulation_params with whitelist."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        original = create_task(
            user_id=self.user_id,
            name="Original",
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
        )

        original.current_status = 2
        original.save(update_fields=['current_status'])
        rerun = create_rerun(
            original.id,
            user_id=self.user_id,
            overrides={
                'simulation_params': {
                    'iterations': 5000,
                    'malicious_key': 'should_be_removed',
                }
            },
        )

        self.assertIn('iterations', rerun.simulation_params)
        self.assertNotIn('malicious_key', rerun.simulation_params)

    def test_rerun_requires_complete_references(self):
        """create_rerun should reject tasks without complete references."""
        from botend.services.task_rerun import create_rerun, TaskRerunError

        # Create incomplete task (missing references)
        incomplete = SimcTask.objects.create(
            user_id=self.user_id,
            name="Incomplete",
            simc_profile_id=999,
            task_type=1,
            current_status=2,
        )

        with self.assertRaises(TaskRerunError) as ctx:
            create_rerun(incomplete.id, user_id=self.user_id)

        self.assertIn("complete references", str(ctx.exception).lower())

    def test_rerun_rejects_pending_and_running_sources(self):
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun, TaskRerunError

        source = create_task(
            user_id=self.user_id, name="Not terminal", profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
        )
        for status in (0, 1):
            source.current_status = status
            source.save(update_fields=['current_status'])
            with self.assertRaises(TaskRerunError) as ctx:
                create_rerun(source.id, user_id=self.user_id)
            self.assertIn("completed or failed", str(ctx.exception).lower())

    def test_non_normal_candidate_rerun_is_normal_and_detached_from_historical_batch(self):
        from botend.models import SimcTaskBatch
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun

        batch = SimcTaskBatch.objects.create(
            user_id=self.user_id, name='Historical comparison', batch_type='comparison', status=2,
        )
        source = create_task(
            user_id=self.user_id, name='Candidate', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id, mode='comparison',
            simulation_params={'iterations': 5000},
            mode_params={'candidate_type': 'talent_override', 'talent_override': 'ABC'},
            candidate_label='talent ABC', batch_id=batch.id,
        )
        source.current_status = 2
        source.save(update_fields=['current_status'])

        rerun = create_rerun(source.id, user_id=self.user_id)

        self.assertEqual(rerun.mode, 'normal')
        self.assertIsNone(rerun.batch_id)
        self.assertEqual(rerun.simulation_params, source.simulation_params)
        self.assertEqual(rerun.mode_params, source.mode_params)
        self.assertEqual(rerun.profile_version_id, source.profile_version_id)
        self.assertEqual(rerun.template_version_id, source.template_version_id)
        self.assertEqual(rerun.apl_version_id, source.apl_version_id)
        source.refresh_from_db()
        self.assertEqual(source.mode, 'comparison')
        self.assertEqual(source.batch_id, batch.id)


class SimulationRunModelTests(TestCase):
    """Test SimulationRun model existence."""

    def test_simulation_run_model_exists(self):
        """GREEN: SimulationRun model should exist."""
        from botend.models import SimulationRun
        self.assertTrue(hasattr(SimulationRun, '_meta'))
