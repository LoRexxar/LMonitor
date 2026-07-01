from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .client import WagoDB2Client
from .schema import WagoDB2Schema
from .types import DB2Object, DB2RecordRef, WagoDB2Graph


class WagoDB2GraphService:
    """Resolve raw Wago DB2 table/record references into reader-facing objects."""

    def __init__(self, *, build: str = '', locale: str = '', client=None, schema=None, max_enrich: int | None = None):
        self.build = str(build or '')
        self.locale = str(locale or '')
        self.client = client or WagoDB2Client(build=self.build, locale=self.locale)
        self.schema = schema or WagoDB2Schema()
        self.max_enrich = max_enrich
        self._enrich_count = 0

    def resolve_hotfix_rows(self, hotfix_rows: list[dict[str, Any]]) -> WagoDB2Graph:
        refs: list[DB2RecordRef] = []
        for row in hotfix_rows or []:
            table = str((row or {}).get('table_name') or '').strip()
            record_id = self._to_int((row or {}).get('record_id') or 0)
            if not table or record_id <= 0:
                continue
            refs.append(DB2RecordRef(
                table=table,
                record_id=record_id,
                push_id=self._to_int((row or {}).get('push_id') or 0),
                build=str((row or {}).get('build') or self.build),
                locale=str((row or {}).get('locale') or self.locale),
                source='hotfix',
            ))
        return self.resolve_record_refs(refs)

    def resolve_record_refs(self, refs: list[DB2RecordRef]) -> WagoDB2Graph:
        graph = WagoDB2Graph()
        object_map: OrderedDict[tuple[str, int | str], DB2Object] = OrderedDict()
        for ref in refs or []:
            graph.table_stats[ref.table] = int(graph.table_stats.get(ref.table) or 0) + 1
            row = ref.row if isinstance(ref.row, dict) else self._fetch_row(ref.table, ref.record_id)
            ref.row = row if isinstance(row, dict) else {}
            obj = self.explain_record(ref)
            if obj is None:
                graph.unresolved_records.append(ref)
                continue
            key = (obj.kind, obj.object_id)
            if key not in object_map:
                object_map[key] = obj
            else:
                existing = object_map[key]
                existing.source_records.extend(obj.source_records)
                existing.related_records.extend(obj.related_records)
                existing.summary_fields = self._merge_summary_fields(existing.summary_fields, obj.summary_fields)
                existing.tags = self._merge_tags(existing.tags, obj.tags)
                if not existing.title and obj.title:
                    existing.title = obj.title
                if not existing.subtitle and obj.subtitle:
                    existing.subtitle = obj.subtitle
        graph.objects = list(object_map.values())
        return graph

    def explain_record(self, ref: DB2RecordRef) -> DB2Object | None:
        kind = self.schema.object_kind_for_table(ref.table)
        if kind == 'spell':
            return self._resolve_spell_record(ref)
        if kind == 'quest':
            return self._resolve_quest_record(ref)
        if kind == 'item':
            return self._resolve_item_record(ref)
        if kind == 'trait':
            return self._resolve_trait_record(ref)
        if kind == 'mount':
            return self._resolve_mount_record(ref)
        if kind == 'battle_pet':
            return self._resolve_battle_pet_record(ref)
        if kind == 'vehicle':
            return self._resolve_vehicle_record(ref)
        return None

    def _resolve_spell_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        spell_id = self._extract_spell_id(table_key, row, ref.record_id)
        if spell_id <= 0:
            return None
        spell_name_row = self._fetch_row('SpellName', spell_id)
        title = self._first_text(spell_name_row) or self._first_text(row) or f'Spell {spell_id}'
        return DB2Object(
            kind='spell',
            object_id=spell_id,
            title=title,
            category='技能/法术',
            source_records=[ref],
            summary_fields=self._summary_fields_for_row(ref.table, row),
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '技能'],
        )

    def _resolve_trait_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        trait_id = self._to_int(row.get('TraitDefinitionID') or row.get('TraitNodeID') or row.get('ID') or ref.record_id)
        spell_id = self._to_int(row.get('SpellID') or 0)
        title = self._first_text(row)
        if spell_id > 0:
            spell_name = self._first_text(self._fetch_row('SpellName', spell_id))
            title = title or spell_name
        title = title or f'天赋对象 {trait_id or ref.record_id}'
        fields = self._summary_fields_for_row(ref.table, row)
        if spell_id > 0:
            spell_name = self._first_text(self._fetch_row('SpellName', spell_id))
            fields.append({'label': '关联技能', 'value': f'{spell_name or "Spell"} #{spell_id}'})
        return DB2Object(
            kind='trait',
            object_id=trait_id or ref.record_id,
            title=title,
            category='天赋',
            source_records=[ref],
            summary_fields=fields,
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '天赋'],
        )

    def _resolve_quest_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        quest_id = self._to_int(row.get('QuestID') or 0)
        if quest_id <= 0 and table_key == 'questv2':
            quest_id = self._to_int(row.get('ID') or ref.record_id)
        if quest_id <= 0:
            return None
        quest_row = row if table_key == 'questv2' else self._fetch_row('QuestV2', quest_id)
        title = self._first_text(quest_row) or self._first_text(row) or f'Quest {quest_id}'
        return DB2Object(
            kind='quest',
            object_id=quest_id,
            title=title,
            category='任务',
            source_records=[ref],
            summary_fields=self._summary_fields_for_row(ref.table, row),
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '任务'],
        )

    def _resolve_item_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        item_id = self._to_int(row.get('ParentItemID') or row.get('ItemID') or 0)
        if item_id <= 0 and table_key in ('item', 'itemsparse'):
            item_id = self._to_int(row.get('ID') or ref.record_id)
        object_id = item_id or ref.record_id
        item_row = row if table_key in ('item', 'itemsparse') else (self._fetch_row('ItemSparse', object_id) or self._fetch_row('Item', object_id))
        title = self._first_text(item_row) or self._first_text(row) or f'Item {object_id}'
        fields = self._summary_fields_for_row(ref.table, row)
        spell_id = self._to_int(row.get('SpellID') or 0)
        if spell_id > 0:
            spell_name = self._first_text(self._fetch_row('SpellName', spell_id))
            fields.append({'label': '关联技能', 'value': f'{spell_name or "Spell"} #{spell_id}'})
        return DB2Object(
            kind='item',
            object_id=object_id,
            title=title,
            category='物品/装备',
            source_records=[ref],
            summary_fields=fields,
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '物品'],
        )

    def _resolve_mount_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        mount_id = self._to_int(row.get('MountID') or row.get('ID') or ref.record_id)
        if mount_id <= 0:
            return None
        mount_row = row if table_key == 'mount' else self._fetch_row('Mount', mount_id)
        title = self._first_text(mount_row) or self._first_text(row) or f'坐骑 #{mount_id}'
        fields = self._summary_fields_for_row(ref.table, row)
        source_spell_id = self._to_int(row.get('SourceSpellID') or (mount_row or {}).get('SourceSpellID') or 0)
        if source_spell_id > 0:
            spell_name = self._first_text(self._fetch_row('SpellName', source_spell_id))
            fields.append({'label': '来源技能', 'value': f'{spell_name or "Spell"} #{source_spell_id}'})
        display_id = self._to_int(row.get('CreatureDisplayInfoID') or (mount_row or {}).get('CreatureDisplayInfoID') or 0)
        if display_id > 0:
            fields.append({'label': '生物外观 ID', 'value': str(display_id)})
        return DB2Object(
            kind='mount',
            object_id=mount_id,
            title=title,
            category='坐骑',
            source_records=[ref],
            summary_fields=fields,
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '坐骑'],
        )

    def _resolve_battle_pet_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        species_id = self._to_int(row.get('SpeciesID') or row.get('BattlePetSpeciesID') or row.get('ID') or ref.record_id)
        if species_id <= 0:
            return None
        species_row = row if table_key == 'battlepetspecies' else self._fetch_row('BattlePetSpecies', species_id)
        title = self._first_text(species_row) or self._first_text(row) or f'战斗宠物 #{species_id}'
        fields = self._summary_fields_for_row(ref.table, row)
        creature_id = self._to_int(row.get('CreatureID') or (species_row or {}).get('CreatureID') or 0)
        if creature_id > 0:
            fields.append({'label': '生物 ID', 'value': str(creature_id)})
        return DB2Object(
            kind='battle_pet',
            object_id=species_id,
            title=title,
            category='战斗宠物',
            source_records=[ref],
            summary_fields=fields,
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '战斗宠物'],
        )

    def _resolve_vehicle_record(self, ref: DB2RecordRef) -> DB2Object | None:
        row = ref.row or {}
        table_key = self.schema.table_key(ref.table)
        vehicle_id = self._to_int(row.get('VehicleID') or row.get('ID') or ref.record_id)
        if table_key == 'vehicleseat' and self._to_int(row.get('VehicleID') or 0) <= 0:
            vehicle_id = self._to_int(row.get('VehicleSeatID') or row.get('ID') or ref.record_id)
        if vehicle_id <= 0:
            return None
        title = self._first_text(row) or (f'载具 #{vehicle_id}' if table_key == 'vehicle' else f'载具座位 #{vehicle_id}')
        fields = self._summary_fields_for_row(ref.table, row)
        return DB2Object(
            kind='vehicle',
            object_id=vehicle_id,
            title=title,
            category='载具/交互',
            source_records=[ref],
            summary_fields=fields,
            raw_fields=self._raw_fields(row),
            tags=[self._tag_for_table(ref.table) or '载具'],
        )

    def _fetch_row(self, table: str, record_id: int) -> dict[str, Any]:
        if self.max_enrich is not None and self._enrich_count >= int(self.max_enrich):
            return {}
        self._enrich_count += 1
        try:
            row = self.client.get_row_by_id(table, int(record_id or 0))
        except Exception:
            row = {}
        return row if isinstance(row, dict) else {}

    def _summary_fields_for_row(self, table: str, row: dict[str, Any]) -> list[dict[str, str]]:
        preferred = [
            'ObjectiveText_lang', 'Description_lang', 'AuraDescription_lang', 'Text_lang',
            'EffectIndex', 'EffectBasePointsF', 'EffectBasePoints', 'EffectBonusCoefficient',
            'BonusCoefficientFromAP', 'Coefficient', 'PvpMultiplier', 'SpellID', 'QuestID',
            'ParentItemID', 'ItemID', 'TriggerType', 'TraitDefinitionID',
            'SourceSpellID', 'CreatureDisplayInfoID', 'CreatureID', 'SpeciesID', 'SourceTypeEnum',
            'IconFileDataID', 'VehicleID', 'VehicleSeatID', 'Flags', 'FlagsB', 'AttachmentID',
            'CameraEnteringDelay', 'CameraEnteringDuration',
        ]
        out: list[dict[str, str]] = []
        for key in preferred:
            if key not in row:
                continue
            value = row.get(key)
            if self._is_summary_noise_field(key, value):
                continue
            out.append({'label': self.schema.field_label(key), 'value': self._stringify(value)})
        return out

    def _is_summary_noise_field(self, key: str, value: Any) -> bool:
        if value is None or str(value).strip() == '':
            return True
        text = str(value).strip()
        try:
            numeric = float(text)
        except (TypeError, ValueError):
            numeric = None
        if numeric == 0.0:
            # EffectIndex=0 是第一个效果位，有阅读价值；其他 0 大多是默认/内部占位。
            return key != 'EffectIndex'
        if key in {'PvpMultiplier'} and numeric == 1.0:
            return True
        low = key.lower()
        if low.startswith('flags') and numeric == 0.0:
            return True
        if 'camera' in low and numeric == 0.0:
            return True
        return False

    def _raw_fields(self, row: dict[str, Any]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for key, value in (row or {}).items():
            if value is None or str(value) == '':
                continue
            out.append({'label': self.schema.field_label(key), 'name': str(key), 'value': self._stringify(value)})
        return out

    def _extract_spell_id(self, table_key: str, row: dict[str, Any], record_id: int) -> int:
        for key in ('SpellID', 'Spell', 'spellid', 'spell'):
            spell_id = self._to_int(row.get(key) or 0)
            if spell_id > 0:
                return spell_id
        if table_key in ('spell', 'spellname', 'spelldescription', 'spellmisc'):
            spell_id = self._to_int(row.get('ID') or record_id)
            if spell_id > 0:
                return spell_id
        return 0

    def _first_text(self, row: dict[str, Any]) -> str:
        if not isinstance(row, dict):
            return ''
        for key in ('Name_lang', 'Name', 'Display_lang', 'DisplayName_lang', 'Title_lang'):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ('Description_lang', 'AuraDescription_lang', 'Text_lang', 'ObjectiveText_lang'):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''

    def _tag_for_table(self, table: str) -> str:
        key = self.schema.table_key(table)
        if key == 'spelleffect':
            return '技能效果'
        if key == 'questv2clitask':
            return '任务目标'
        if key == 'itemeffect':
            return '物品效果'
        if key == 'mount':
            return '坐骑'
        if key == 'battlepetspecies':
            return '战斗宠物品种'
        if key == 'vehicleseat':
            return '载具座位'
        label = self.schema.TABLE_LABELS.get(key, '')
        return label

    def _merge_summary_fields(self, a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = {(str(x.get('label')), str(x.get('value'))) for x in a or []}
        out = list(a or [])
        for item in b or []:
            key = (str(item.get('label')), str(item.get('value')))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _merge_tags(self, a: list[str], b: list[str]) -> list[str]:
        out: list[str] = []
        for tag in list(a or []) + list(b or []):
            tag = str(tag or '').strip()
            if tag and tag not in out:
                out.append(tag)
        return out

    def _stringify(self, value: Any) -> str:
        text = str(value).strip()
        return text[:520] + '…' if len(text) > 520 else text

    def _to_int(self, value: Any) -> int:
        try:
            return int(str(value).strip() or '0')
        except (TypeError, ValueError):
            return 0
