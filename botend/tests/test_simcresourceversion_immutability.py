"""
Test SimcResourceVersion immutability protection.

Run with: DJANGO_SETTINGS_MODULE=LMonitor.settings_test_sqlite python manage.py test botend.tests.test_simcresourceversion_immutability
"""
from django.test import TestCase
from botend.models import SimcResourceVersion


class SimcResourceVersionImmutabilityTests(TestCase):
    """Test that SimcResourceVersion rejects modifications after creation."""

    def test_modify_payload_after_save_raises_valueerror(self):
        """Version should reject payload modification after creation."""
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='original_hash',
            payload={'content': 'original'},
        )

        original_payload = version.payload

        # Try to modify payload
        version.payload = {'content': 'modified'}

        with self.assertRaises(ValueError) as ctx:
            version.save()

        self.assertIn("immutable", str(ctx.exception).lower())

        # Verify DB value unchanged
        version.refresh_from_db()
        self.assertEqual(version.payload, original_payload)

    def test_modify_resource_type_after_save_raises_valueerror(self):
        """Version should reject resource_type modification."""
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='hash1',
            payload={'content': 'data'},
        )

        version.resource_type = 'apl'

        with self.assertRaises(ValueError):
            version.save()

        version.refresh_from_db()
        self.assertEqual(version.resource_type, 'profile')

    def test_modify_resource_id_after_save_raises_valueerror(self):
        """Version should reject resource_id modification."""
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='hash1',
            payload={'content': 'data'},
        )

        version.resource_id = 456

        with self.assertRaises(ValueError):
            version.save()

        version.refresh_from_db()
        self.assertEqual(version.resource_id, 123)

    def test_modify_content_hash_after_save_raises_valueerror(self):
        """Version should reject content_hash modification."""
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='original_hash',
            payload={'content': 'data'},
        )

        version.content_hash = 'modified_hash'

        with self.assertRaises(ValueError):
            version.save()

        version.refresh_from_db()
        self.assertEqual(version.content_hash, 'original_hash')

    def test_no_change_save_succeeds(self):
        """Version should allow save() when no fields changed."""
        version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=123,
            content_hash='hash1',
            payload={'content': 'data'},
        )

        # Save without changes should succeed
        version.save()

        version.refresh_from_db()
        self.assertEqual(version.resource_type, 'profile')
        self.assertEqual(version.payload, {'content': 'data'})

    def test_create_new_version_succeeds(self):
        """Creating a new version (no pk) should always succeed."""
        version = SimcResourceVersion(
            resource_type='apl',
            resource_id=999,
            content_hash='new_hash',
            payload={'content': 'new_data'},
        )

        version.save()

        self.assertIsNotNone(version.pk)
        self.assertEqual(version.payload, {'content': 'new_data'})
