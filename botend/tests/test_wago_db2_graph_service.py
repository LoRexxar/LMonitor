from django.test import SimpleTestCase

from botend.services.wago_db2.types import DB2RecordRef
from botend.services.wago_db2.graph import WagoDB2GraphService


class FakeWagoDB2Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_row_by_id(self, table, record_id):
        key = (str(table), int(record_id))
        self.calls.append(key)
        return self.rows.get(key) or {}

    def get_rows_by_field(self, table, field, value):
        value = str(value)
        out = []
        for (row_table, _), row in self.rows.items():
            if row_table.lower() != str(table).lower():
                continue
            if str((row or {}).get(field) or '') == value:
                out.append(row)
        return out


class WagoDB2GraphServiceTests(SimpleTestCase):
    def test_resolve_hotfix_spell_effect_groups_under_spell_object(self):
        client = FakeWagoDB2Client({
            ('SpellEffect', 1001): {
                'ID': 1001,
                'SpellID': 19434,
                'EffectIndex': 0,
                'EffectBasePointsF': '123',
                'BonusCoefficientFromAP': '1.25',
            },
            ('SpellName', 19434): {
                'ID': 19434,
                'Name_lang': '瞄准射击',
            },
        })
        service = WagoDB2GraphService(build='68367', locale='zhCN', client=client)

        graph = service.resolve_hotfix_rows([
            {'table_name': 'SpellEffect', 'record_id': 1001, 'push_id': 109505},
        ])

        self.assertEqual(len(graph.objects), 1)
        obj = graph.objects[0]
        self.assertEqual(obj.kind, 'spell')
        self.assertEqual(obj.object_id, 19434)
        self.assertEqual(obj.title, '瞄准射击')
        self.assertIn('技能效果', obj.tags)
        self.assertEqual(obj.source_records[0].table, 'SpellEffect')
        self.assertEqual(obj.source_records[0].record_id, 1001)
        self.assertTrue(any(f['label'] == '攻强系数' and f['value'] == '1.25' for f in obj.summary_fields))

    def test_resolve_quest_cli_task_groups_under_quest_object(self):
        client = FakeWagoDB2Client({
            ('QuestV2CliTask', 300): {
                'ID': 300,
                'QuestID': 12345,
                'ObjectiveText_lang': '激活三座信标',
            },
            ('QuestV2', 12345): {
                'ID': 12345,
                'Title_lang': '修复信标',
            },
        })
        service = WagoDB2GraphService(build='68367', locale='zhCN', client=client)

        graph = service.resolve_hotfix_rows([
            {'table_name': 'QuestV2CliTask', 'record_id': 300, 'push_id': 109505},
        ])

        self.assertEqual(len(graph.objects), 1)
        obj = graph.objects[0]
        self.assertEqual(obj.kind, 'quest')
        self.assertEqual(obj.object_id, 12345)
        self.assertEqual(obj.title, '修复信标')
        self.assertIn('任务目标', obj.tags)
        self.assertTrue(any(f['label'] == '任务目标' and f['value'] == '激活三座信标' for f in obj.summary_fields))

    def test_resolve_item_effect_links_item_and_spell_context(self):
        client = FakeWagoDB2Client({
            ('ItemEffect', 500): {
                'ID': 500,
                'ParentItemID': 19019,
                'SpellID': 77777,
                'TriggerType': 0,
            },
            ('ItemSparse', 19019): {
                'ID': 19019,
                'Display_lang': '奥术饰品',
            },
            ('SpellName', 77777): {
                'ID': 77777,
                'Name_lang': '奥术爆发',
            },
        })
        service = WagoDB2GraphService(build='68367', locale='zhCN', client=client)

        graph = service.resolve_hotfix_rows([
            {'table_name': 'ItemEffect', 'record_id': 500, 'push_id': 109505},
        ])

        self.assertEqual(len(graph.objects), 1)
        obj = graph.objects[0]
        self.assertEqual(obj.kind, 'item')
        self.assertEqual(obj.object_id, 19019)
        self.assertEqual(obj.title, '奥术饰品')
        self.assertIn('物品效果', obj.tags)
        self.assertTrue(any(f['label'] == '关联技能' and f['value'] == '奥术爆发 #77777' for f in obj.summary_fields))

    def test_unknown_table_remains_unresolved_with_loaded_row(self):
        client = FakeWagoDB2Client({
            ('TotallyUnknownTable', 999): {
                'ID': 999,
                'Flags': 12,
            },
        })
        service = WagoDB2GraphService(build='68367', locale='zhCN', client=client)

        graph = service.resolve_hotfix_rows([
            {'table_name': 'TotallyUnknownTable', 'record_id': 999, 'push_id': 109505},
        ])

        self.assertEqual(graph.objects, [])
        self.assertEqual(len(graph.unresolved_records), 1)
        self.assertEqual(graph.unresolved_records[0].table, 'TotallyUnknownTable')
        self.assertEqual(graph.unresolved_records[0].row['Flags'], 12)

    def test_resolve_record_refs_accepts_generic_refs_not_only_hotfix_rows(self):
        client = FakeWagoDB2Client({
            ('SpellName', 19434): {
                'ID': 19434,
                'Name_lang': '瞄准射击',
            },
        })
        service = WagoDB2GraphService(build='68367', locale='zhCN', client=client)

        graph = service.resolve_record_refs([DB2RecordRef(table='SpellName', record_id=19434)])

        self.assertEqual(len(graph.objects), 1)
        self.assertEqual(graph.objects[0].kind, 'spell')
        self.assertEqual(graph.objects[0].title, '瞄准射击')
