from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.management.commands.fetch_item_metadata import Command
from botend.models import WowItemSnapshot


class FetchItemMetadataFailureTests(TestCase):
    @patch('botend.management.commands.fetch_item_metadata.WowItemSnapshot.objects.update_or_create')
    @patch('botend.management.commands.fetch_item_metadata.WowItemSnapshot.objects.filter')
    def test_failed_authority_requests_do_not_create_empty_snapshot(self, filter_mock, update_or_create_mock):
        filter_mock.return_value = []
        update_or_create_mock.return_value = (Mock(), True)
        command = Command()
        command._fetch_wowhead_cn = Mock(return_value={})
        command._fetch_wowhead_en = Mock(return_value={})

        command.handle(
            item_id=[999999999], limit=0, sleep=0, force=False,
            season_id=0, ptr=False,
        )

        update_or_create_mock.assert_not_called()

    @patch('botend.management.commands.fetch_item_metadata.WowItemSnapshot.objects.update_or_create')
    @patch('botend.management.commands.fetch_item_metadata.WowItemSnapshot.objects.filter')
    def test_fact_free_authority_payloads_do_not_create_empty_snapshot(self, filter_mock, update_or_create_mock):
        filter_mock.return_value = []
        update_or_create_mock.return_value = (Mock(), True)
        command = Command()
        command._fetch_wowhead_cn = Mock(return_value={
            'name_zh': '', 'description_zh': '', 'icon': '', 'quality': 0,
        })
        command._fetch_wowhead_en = Mock(return_value={
            'name': '', 'description': '', 'icon': '', 'quality': 0,
        })

        command.handle(
            item_id=[999999999], limit=0, sleep=0, force=False,
            season_id=0, ptr=False,
        )

        update_or_create_mock.assert_not_called()

    def test_fact_free_authority_payload_preserves_existing_snapshot(self):
        original_time = timezone.now() - timedelta(days=1)
        row = WowItemSnapshot.objects.create(
            item_id=999999998,
            name='Existing Item',
            name_zh='现有装备',
            description='',
            description_zh='已有中文描述',
            icon='inv_existing_item',
            quality=4,
            source='existing_source',
            updated_at=original_time,
        )
        before = (
            row.name, row.name_zh, row.description, row.description_zh,
            row.icon, row.quality, row.source, row.updated_at,
        )
        command = Command()
        command._fetch_wowhead_cn = Mock(return_value={})
        command._fetch_wowhead_en = Mock(return_value={
            'name': '', 'description': '', 'icon': '', 'quality': 0,
        })

        command.handle(
            item_id=[row.item_id], limit=0, sleep=0, force=False,
            season_id=0, ptr=False,
        )

        row.refresh_from_db()
        after = (
            row.name, row.name_zh, row.description, row.description_zh,
            row.icon, row.quality, row.source, row.updated_at,
        )
        self.assertEqual(after, before)
