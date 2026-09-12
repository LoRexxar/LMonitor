from unittest.mock import Mock, patch

from django.test import TestCase

from botend.management.commands.fetch_item_metadata import Command


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
