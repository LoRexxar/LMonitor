"""
SimC Composer Service - Phase 1 Semantic Slot Resolution

Contract: SimC input is NOT four-section mechanical assembly, but semantic slot parsing + template rendering.

Slots: simulation_options, player_identity, talents, equipment, stat_overrides, action_list, output_options
Process: normalize sources → source arbitration (one source per slot) → render via template placeholders

Key rules:
- Manual/Addon equipment blocks default equipment load
- Armory occupies equipment slot even when content empty (no fallback)
- User class/spec vs BNet: consistent merge, conflict reject
- Explicit empty APL stays empty (no fallback)
- One actor only in final content
- Execution is assembled at run time from immutable resource versions; Task rows do not store frozen SimC bodies.
- No client-provided _bnet_* fields trusted; server validates Battle.net
- Templates filtered by user_id + active status
- No arbitrary .first() fallback; 0 or >1 defaults fail explicitly
"""
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from django.db import models
from botend.models import SimcContentTemplate, SimcApl
from botend.services.simc_player_config import SPEC_CLASS


@dataclass
class SlotValue:
    """Resolved slot value with source tracking."""
    content: str
    source: str  # e.g., 'user_manual', 'battlenet_armory', 'addon_export', 'default_template', 'user_explicit_empty'
    source_id: Optional[int] = None  # Template ID if from DB
    source_version: str = ''  # Git hash or version for audit
    content_hash: str = ''  # SHA256 of content for change detection


@dataclass
class SlotResolution:
    """Result of slot arbitration for a single slot."""
    slot_name: str
    value: Optional[SlotValue]
    status: str  # 'resolved', 'empty', 'conflict', 'missing', 'explicit_empty'
    error: str = ''

    def to_manifest_entry(self) -> Dict[str, Any]:
        """Convert to manifest entry with full metadata."""
        entry = {
            'status': self.status,
            'source': self.value.source if self.value else 'none',
            'provided_by': self.value.source if self.value else None,
            'content_hash': self.value.content_hash if self.value else None,
            'source_id': self.value.source_id if self.value else None,
            'source_version': self.value.source_version if self.value else None,
        }
        if self.error:
            entry['error'] = self.error
        return entry


@dataclass
class CompositionManifest:
    """Frozen manifest v2: records full slot metadata."""
    manifest_version: str = 'v2'

    # Full slot metadata (status/source/hash per slot)
    slots: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Template metadata
    base_template_id: Optional[int] = None
    base_template_version: str = ''
    base_template_hash: str = ''

    # Composition metadata
    created_at: str = ''
    user_id: int = 0

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)


class SimcComposer:
    """Compose frozen SimC input from semantic slot resolution."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.slots: Dict[str, SlotResolution] = {}
        self.manifest = CompositionManifest()

    def compose(self, request_data: Dict[str, Any]) -> tuple[Optional[str], Optional[CompositionManifest], Optional[str]]:
        """
        Main composition pipeline.

        Returns: (final_simc_content, manifest, error_message)
        """
        options_error = self._validate_simulation_options(request_data)
        if options_error:
            return None, None, options_error

        # Step 1: Resolve equipment slot FIRST (identity depends on whether equipment has actor)
        equipment_result = self._resolve_equipment(request_data)
        if equipment_result.status == 'conflict':
            return None, None, equipment_result.error
        self.slots['equipment'] = equipment_result

        # Step 2: Resolve player identity slot (class/spec) - checks equipment for actor
        identity_result = self._resolve_player_identity(request_data)
        if identity_result.status == 'conflict':
            return None, None, identity_result.error
        self.slots['player_identity'] = identity_result

        # Step 3: Resolve talents slot
        talents_result = self._resolve_talents(request_data)
        self.slots['talents'] = talents_result

        # Step 4: Resolve action_list slot
        apl_result = self._resolve_action_list(request_data)
        if apl_result.status == 'conflict':
            return None, None, apl_result.error
        self.slots['action_list'] = apl_result

        # Step 5: Resolve simulation_options slot
        sim_options_result = self._resolve_simulation_options(request_data)
        self.slots['simulation_options'] = sim_options_result

        # Step 6: Resolve stat_overrides slot
        stat_overrides_result = self._resolve_stat_overrides(request_data)
        self.slots['stat_overrides'] = stat_overrides_result

        # Step 7: Resolve output_options slot
        output_options_result = self._resolve_output_options(request_data)
        self.slots['output_options'] = output_options_result

        # Step 8: Load base template
        base_template_content, base_template_id, base_template_version = self._load_base_template(request_data)
        if not base_template_content:
            return None, None, "未找到可用的基础模板"

        self._base_template_content = base_template_content
        self.manifest.base_template_id = base_template_id
        self.manifest.base_template_version = base_template_version

        # Step 9: Render final content via placeholders
        final_content = self._render_template(base_template_content, request_data)

        # Step 10: Validate single actor
        actor_count = self._count_actors(final_content)
        if actor_count != 1:
            return None, None, f"最终内容必须包含且仅包含一个角色定义，当前检测到 {actor_count} 个"

        # Step 11: Validate all placeholders replaced
        unknown_placeholders = self._find_unknown_placeholders(final_content)
        if unknown_placeholders:
            return None, None, f"最终内容包含未替换的占位符: {', '.join(unknown_placeholders)}"

        # Step 12: Build manifest with full slot metadata
        self._build_manifest()

        return final_content, self.manifest, None

    def _resolve_player_identity(self, request_data: Dict[str, Any]) -> SlotResolution:
        """
        Resolve player_identity slot: class + spec + name.

        User spec vs BNet spec: consistent merge, conflict reject.

        NOTE: This is called AFTER equipment resolution, so we know if equipment has actor.
        """
        user_spec = (request_data.get('spec') or '').strip().lower()
        player_import_mode = request_data.get('player_import_mode', '').strip()

        # Derive class from spec using authoritative SPEC_CLASS
        derived_class = SPEC_CLASS.get(user_spec) if user_spec else None

        # For battlenet mode, prefer the frozen actor block. The Battle.net identity
        # is source metadata and is only an execution fallback for legacy rows.
        if player_import_mode == 'battlenet':
            player_equipment = (request_data.get('player_equipment') or '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                export_spec = parsed.get('spec', '').strip().lower()
                export_class = parsed.get('class', '').strip().lower()
                if user_spec and export_spec and user_spec != export_spec:
                    return SlotResolution(
                        slot_name='player_identity', value=None, status='conflict',
                        error=f'用户指定的专精 {user_spec} 与 Battle.net 快照专精 {export_spec} 冲突',
                    )
                if derived_class and export_class and derived_class != export_class:
                    return SlotResolution(
                        slot_name='player_identity', value=None, status='conflict',
                        error=f'用户指定的职业 {derived_class} 与 Battle.net 快照职业 {export_class} 冲突',
                    )
                if parsed['identity']:
                    identity = parsed['identity']
                    return SlotResolution(
                        slot_name='player_identity',
                        value=SlotValue(
                            content=identity, source='battlenet_snapshot',
                            content_hash=hashlib.sha256(identity.encode('utf-8')).hexdigest(),
                        ),
                        status='resolved',
                    )
            server_preflight = request_data.get('_server_preflight', {})
            bnet_char = server_preflight.get('character', {})
            bnet_spec = (bnet_char.get('spec') or '').strip().lower()
            bnet_class = (bnet_char.get('class') or '').strip().lower()

            # Check spec conflict
            if user_spec and bnet_spec and user_spec != bnet_spec:
                return SlotResolution(
                    slot_name='player_identity',
                    value=None,
                    status='conflict',
                    error=f'用户指定的专精 {user_spec} 与 Battle.net 角色专精 {bnet_spec} 冲突'
                )

            # Check class conflict
            if derived_class and bnet_class and derived_class != bnet_class:
                return SlotResolution(
                    slot_name='player_identity',
                    value=None,
                    status='conflict',
                    error=f'用户指定的职业 {derived_class} (来自专精 {user_spec}) 与 Battle.net 角色职业 {bnet_class} 冲突'
                )

            # Use BNet spec if user didn't provide one
            if not user_spec and bnet_spec:
                user_spec = bnet_spec
                derived_class = SPEC_CLASS.get(bnet_spec)

            # Battle.net does not expose a portable SimC equipment export. Freeze
            # the authoritative armory import instruction itself; SimC resolves the
            # active character/equipment when the immutable task is executed.
            region = str(request_data.get('battlenet_region') or '').strip().lower()
            realm = str(request_data.get('battlenet_realm') or '').strip()
            character = str(request_data.get('battlenet_character') or '').strip()
            if not region or not realm or not character:
                return SlotResolution(
                    slot_name='player_identity', value=None, status='missing',
                    error='Battle.net 导入缺少 region、realm 或 character',
                )
            armory_content = f'armory={region},{realm},{character}'
            return SlotResolution(
                slot_name='player_identity',
                value=SlotValue(
                    content=armory_content,
                    source='battlenet_armory',
                    content_hash=hashlib.sha256(armory_content.encode('utf-8')).hexdigest(),
                ),
                status='resolved',
            )

        # For addon/manual export modes, parse and check for conflicts
        if player_import_mode in ('addon_full_export', 'manual_equipment'):
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                export_spec = parsed.get('spec', '').strip().lower()
                export_class = parsed.get('class', '').strip().lower()

                # Check spec conflict
                if user_spec and export_spec and user_spec != export_spec:
                    return SlotResolution(
                        slot_name='player_identity',
                        value=None,
                        status='conflict',
                        error=f'用户指定的专精 {user_spec} 与导出内容中的专精 {export_spec} 冲突'
                    )

                # Check class conflict
                if derived_class and export_class and derived_class != export_class:
                    return SlotResolution(
                        slot_name='player_identity',
                        value=None,
                        status='conflict',
                        error=f'用户指定的职业 {derived_class} 与导出内容中的职业 {export_class} 冲突'
                    )

                # For addon/manual with actor, identity is parsed from export
                if parsed['identity']:
                    content_hash = hashlib.sha256(parsed['identity'].encode('utf-8')).hexdigest()
                    return SlotResolution(
                        slot_name='player_identity',
                        value=SlotValue(
                            content=parsed['identity'],
                            source='addon_export' if player_import_mode == 'addon_full_export' else 'manual_equipment',
                            content_hash=content_hash,
                        ),
                        status='resolved'
                    )

        # Check if equipment slot already has actor definition (for non-parsed modes)
        equipment_resolution = self.slots.get('equipment')
        if equipment_resolution and equipment_resolution.value and equipment_resolution.value.content:
            if player_import_mode not in ('addon_full_export', 'manual_equipment'):
                if self._has_actor_definition(equipment_resolution.value.content):
                    # Equipment has actor, identity merges into it (no separate identity line)
                    content_hash = hashlib.sha256(b'').hexdigest()
                    return SlotResolution(
                        slot_name='player_identity',
                        value=SlotValue(content='', source='merged_into_equipment', content_hash=content_hash),
                        status='resolved'
                    )

        # No actor in equipment, generate standalone identity
        final_spec = user_spec or 'fury'
        final_class = derived_class or 'warrior'
        player_name = request_data.get('battlenet_character') or 'Player'

        identity_content = f'{final_class}="{player_name}"\nspec={final_spec}'
        content_hash = hashlib.sha256(identity_content.encode('utf-8')).hexdigest()

        return SlotResolution(
            slot_name='player_identity',
            value=SlotValue(content=identity_content, source='user_input', content_hash=content_hash),
            status='resolved'
        )

    def _resolve_equipment(self, request_data: Dict[str, Any]) -> SlotResolution:
        """
        Resolve equipment slot with fallback prevention.

        Rules:
        - Manual/Addon equipment blocks default load
        - Armory occupies slot even when empty (no fallback)
        - For battlenet mode, use player_equipment as the equipment content (server should populate from armory)
        - Parse addon/manual exports to extract only equipment lines
        """
        player_import_mode = request_data.get('player_import_mode', '').strip()
        player_equipment = (request_data.get('player_equipment') or '').strip()

        # Manual equipment input - parse to extract only equipment
        if player_import_mode == 'manual_equipment' and player_equipment:
            parsed = self._parse_player_export(player_equipment)
            equipment_content = parsed['equipment']
            content_hash = hashlib.sha256(equipment_content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='equipment',
                value=SlotValue(content=equipment_content, source='manual_equipment', content_hash=content_hash),
                status='resolved'
            )

        # Addon full export - parse to extract only equipment
        if player_import_mode == 'addon_full_export' and player_equipment:
            parsed = self._parse_player_export(player_equipment)
            equipment_content = parsed['equipment']
            content_hash = hashlib.sha256(equipment_content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='equipment',
                value=SlotValue(content=equipment_content, source='addon_export', content_hash=content_hash),
                status='resolved'
            )

        # Battle.net snapshot owns the equipment slot. Parse the actor block so its
        # identity/talent lines are rendered exactly once in their semantic slots.
        if player_import_mode == 'battlenet':
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                equipment_content = parsed['equipment']
                content_hash = hashlib.sha256(equipment_content.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=equipment_content, source='battlenet_snapshot',
                        content_hash=content_hash,
                    ),
                    status='resolved'
                )
            else:
                # Armory mode but empty - slot occupied, no fallback
                content_hash = hashlib.sha256(b'').hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(content='', source='battlenet_armory', content_hash=content_hash),
                    status='empty'
                )

        # Attribute-only mode may carry a frozen player baseline selected by the
        # user/API. A provided baseline owns the equipment slot and must block
        # mutable default-equipment fallback, exactly like a manual export.
        if player_import_mode == 'attribute_only':
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                equipment_content = parsed['equipment']
                content_hash = hashlib.sha256(equipment_content.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=equipment_content,
                        source='attribute_frozen_baseline',
                        content_hash=content_hash,
                    ),
                    status='resolved',
                )
            spec = request_data.get('spec', 'fury')
            default_equipment = self._load_default_equipment(spec)
            if default_equipment:
                content_hash = hashlib.sha256(default_equipment.content.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=default_equipment.content,
                        source='default_template',
                        source_id=default_equipment.id,
                        source_version=default_equipment.sync_version,
                        content_hash=content_hash
                    ),
                    status='resolved'
                )
            else:
                return SlotResolution(
                    slot_name='equipment',
                    value=None,
                    status='missing',
                    error=f'专精 {spec} 没有可用的默认装备模板'
                )

        # Default mode uses default equipment
        if player_import_mode == 'default':
            spec = request_data.get('spec', 'fury')
            default_equipment = self._load_default_equipment(spec)
            if default_equipment:
                content_hash = hashlib.sha256(default_equipment.content.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=default_equipment.content,
                        source='default_template',
                        source_id=default_equipment.id,
                        source_version=default_equipment.sync_version,
                        content_hash=content_hash
                    ),
                    status='resolved'
                )
            else:
                return SlotResolution(
                    slot_name='equipment',
                    value=None,
                    status='missing',
                    error=f'专精 {spec} 没有可用的默认装备模板'
                )

        # No equipment provided and no default fallback
        content_hash = hashlib.sha256(b'').hexdigest()
        return SlotResolution(
            slot_name='equipment',
            value=None,
            status='empty'
        )

    def _resolve_talents(self, request_data: Dict[str, Any]) -> SlotResolution:
        """Resolve talents slot."""
        player_import_mode = request_data.get('player_import_mode', '').strip()

        # Frozen player exports own their talents; the workbench APL remains separate.
        if player_import_mode in ('addon_full_export', 'manual_equipment', 'battlenet'):
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                if parsed['talents']:
                    content_hash = hashlib.sha256(parsed['talents'].encode('utf-8')).hexdigest()
                    source = {
                        'addon_full_export': 'addon_export',
                        'manual_equipment': 'manual_equipment',
                        'battlenet': 'battlenet_snapshot',
                    }[player_import_mode]
                    return SlotResolution(
                        slot_name='talents',
                        value=SlotValue(
                            content=parsed['talents'],
                            source=source,
                            content_hash=content_hash,
                        ),
                        status='resolved'
                    )

        talent = (request_data.get('talent') or '').strip()

        if talent:
            content_hash = hashlib.sha256(f'talents={talent}'.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='talents',
                value=SlotValue(content=f'talents={talent}', source='user_input', content_hash=content_hash),
                status='resolved'
            )

        return SlotResolution(
            slot_name='talents',
            value=None,
            status='empty'
        )

    def _resolve_action_list(self, request_data: Dict[str, Any]) -> SlotResolution:
        """
        Resolve action_list slot.

        Explicit empty APL (override_action_list='') stays empty (status='explicit_empty', no fallback).
        Missing APL has status='empty'.
        """
        player_import_mode = request_data.get('player_import_mode', '').strip()
        override_apl = request_data.get('override_action_list')
        selected_apl_id = request_data.get('selected_apl_id')

        # For addon/manual export, check parsed actions
        if player_import_mode in ('addon_full_export', 'manual_equipment'):
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                if parsed['actions']:
                    content_hash = hashlib.sha256(parsed['actions'].encode('utf-8')).hexdigest()
                    return SlotResolution(
                        slot_name='action_list',
                        value=SlotValue(
                            content=parsed['actions'],
                            source='addon_export' if player_import_mode == 'addon_full_export' else 'manual_equipment',
                            content_hash=content_hash,
                        ),
                        status='resolved'
                    )

        # Explicit empty APL - maintain distinction from missing
        if override_apl is not None and override_apl == '':
            content_hash = hashlib.sha256(b'').hexdigest()
            return SlotResolution(
                slot_name='action_list',
                value=SlotValue(content='', source='user_explicit_empty', content_hash=content_hash),
                status='explicit_empty'  # Different from 'empty' (missing)
            )

        # User-provided override
        if override_apl:
            content_hash = hashlib.sha256(override_apl.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='action_list',
                value=SlotValue(content=override_apl, source='user_override', content_hash=content_hash),
                status='resolved'
            )

        # Selected APL by ID - must check user_id isolation
        if selected_apl_id:
            try:
                apl = SimcApl.objects.filter(
                    id=selected_apl_id,
                    is_active=True,
                ).filter(
                    models.Q(is_system=True, owner_user_id__isnull=True)
                    | models.Q(is_system=False, owner_user_id=self.user_id)
                ).first()
                if apl:
                    content_hash = hashlib.sha256(apl.content.encode('utf-8')).hexdigest()
                    return SlotResolution(
                        slot_name='action_list',
                        value=SlotValue(
                            content=apl.content,
                            source='selected_apl',
                            source_id=apl.id,
                            source_version=apl.sync_version,
                            content_hash=content_hash
                        ),
                        status='resolved'
                    )
                else:
                    return SlotResolution(
                        slot_name='action_list',
                        value=None,
                        status='missing',
                        error=f'APL ID {selected_apl_id} 不存在或无权访问'
                    )
            except Exception as e:
                return SlotResolution(
                    slot_name='action_list',
                    value=None,
                    status='missing',
                    error=f'APL ID {selected_apl_id} 不存在'
                )

        # Auto-select unique default APL by spec - use SPEC_CLASS mapping
        spec = request_data.get('spec', 'fury').lower()
        class_name = SPEC_CLASS.get(spec, 'warrior')
        spec_key = f'{class_name}_{spec}'

        apls = SimcApl.objects.filter(
            spec=spec_key,
            is_active=True,
            is_system=True,
            source='simc_upstream',  # Only global defaults
            owner_user_id__isnull=True
        )

        count = apls.count()
        if count == 0:
            # Missing APL
            return SlotResolution(
                slot_name='action_list',
                value=None,
                status='empty'
            )
        elif count == 1:
            apl = apls.first()
            content_hash = hashlib.sha256(apl.content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='action_list',
                value=SlotValue(
                    content=apl.content,
                    source='auto_selected_default',
                    source_id=apl.id,
                    source_version=apl.sync_version,
                    content_hash=content_hash
                ),
                status='resolved'
            )
        else:
            # Multiple defaults - fail explicitly, no arbitrary .first()
            return SlotResolution(
                slot_name='action_list',
                value=None,
                status='conflict',
                error=f'专精 {spec} 存在 {count} 个启用的默认 APL，请明确选择其中一个'
            )

    def _resolve_simulation_options(self, request_data: Dict[str, Any]) -> SlotResolution:
        """Resolve and render the complete supported simulation option contract."""
        options = [
            f"fight_style={request_data.get('fight_style', 'Patchwerk')}",
            f"max_time={request_data.get('time', 300)}",
            f"desired_targets={request_data.get('target_count', 1)}",
            'optimal_raid=0',
            'override.battle_shout=1',
            f"iterations={request_data.get('iterations', 10000)}",
        ]
        if request_data.get('target_error') is not None:
            options.append(f"target_error={request_data['target_error']}")
        if request_data.get('vary_combat_length') is not None:
            options.append(f"vary_combat_length={request_data['vary_combat_length']}")
        if request_data.get('enemy_type'):
            options.append(f"enemy={request_data['enemy_type']}")
        options.append('threads=4')

        content = '\n'.join(options)
        content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        return SlotResolution(
            slot_name='simulation_options',
            value=SlotValue(content=content, source='user_input', content_hash=content_hash),
            status='resolved'
        )

    @staticmethod
    def _validate_simulation_options(request_data: Dict[str, Any]) -> str:
        """Reject invalid values rather than allowing persisted options to become code."""
        def integer(name, default, minimum, maximum):
            value = request_data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                return f'{name} 必须是 {minimum} 到 {maximum} 之间的整数'
            return ''

        def number(name, minimum, maximum):
            value = request_data.get(name)
            if value is None:
                return ''
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f'{name} 必须是数字'
            value = float(value)
            if not math.isfinite(value) or not minimum <= value <= maximum:
                return f'{name} 必须在 {minimum} 到 {maximum} 之间'
            return ''

        for error in (
            integer('iterations', 10000, 1, 100000000),
            integer('time', 300, 1, 86400),
            integer('target_count', 1, 1, 1000),
            number('target_error', 0, 1),
            number('vary_combat_length', 0, 1),
        ):
            if error:
                return error
        for name, default in (('fight_style', 'Patchwerk'), ('enemy_type', '')):
            value = request_data.get(name, default)
            if value is None and name == 'enemy_type':
                value = ''
            if not isinstance(value, str) or (value and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value)):
                return f'{name} 包含无效值'
        return ''

    def _resolve_stat_overrides(self, request_data: Dict[str, Any]) -> SlotResolution:
        """Resolve stat_overrides slot (gear_crit, gear_haste, etc)."""
        overrides = []

        gear_crit = request_data.get('gear_crit')
        gear_haste = request_data.get('gear_haste')
        gear_mastery = request_data.get('gear_mastery')
        gear_versatility = request_data.get('gear_versatility')

        if gear_crit is not None:
            overrides.append(f'gear_crit_rating={gear_crit}')
        if gear_haste is not None:
            overrides.append(f'gear_haste_rating={gear_haste}')
        if gear_mastery is not None:
            overrides.append(f'gear_mastery_rating={gear_mastery}')
        if gear_versatility is not None:
            overrides.append(f'gear_versatility_rating={gear_versatility}')

        if overrides:
            content = '\n'.join(overrides)
            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='stat_overrides',
                value=SlotValue(content=content, source='user_input', content_hash=content_hash),
                status='resolved'
            )

        return SlotResolution(
            slot_name='stat_overrides',
            value=None,
            status='empty'
        )

    def _resolve_output_options(self, request_data: Dict[str, Any]) -> SlotResolution:
        """Resolve output_options slot (html report path)."""
        result_file = request_data.get('_result_file_path', 'result.html')

        content_hash = hashlib.sha256(f'html={result_file}'.encode('utf-8')).hexdigest()
        return SlotResolution(
            slot_name='output_options',
            value=SlotValue(content=f'html={result_file}', source='system_generated', content_hash=content_hash),
            status='resolved'
        )

    def _load_template_with_access_check(self, template_id: int, template_type: int) -> Optional[SimcContentTemplate]:
        """
        Load template with owner-based access control.

        Rules:
        - Global templates (owner_user_id=None) are readable by all
        - User-owned templates (owner_user_id=X) are only readable by that user
        - Explicitly invalid IDs must not fall back silently
        """
        try:
            template = SimcContentTemplate.objects.get(
                id=template_id,
                template_type=template_type,
                is_active=True
            )

            # Check access: global (owner_user_id=None) or user-owned
            if template.owner_user_id is None:
                return template
            elif template.owner_user_id == self.user_id:
                return template
            else:
                # Template belongs to another user - access denied
                return None

        except SimcContentTemplate.DoesNotExist:
            return None

    def _load_base_template(self, request_data: Dict[str, Any]) -> tuple[Optional[str], Optional[int], str]:
        """
        Load base template.

        Priority: explicit base_template_content > base_template_id > spec default
        """
        # User-edited base template content
        base_template_content = request_data.get('base_template_content')
        if base_template_content:
            base_template_id = request_data.get('base_template_id')
            return base_template_content, base_template_id, 'user_edited'

        # Explicit base_template_id with access control
        base_template_id = request_data.get('base_template_id')
        if base_template_id:
            template = self._load_template_with_access_check(
                base_template_id,
                SimcContentTemplate.TYPE_BASE_TEMPLATE
            )
            if template:
                return template.content, template.id, template.sync_version
            else:
                # Explicit ID but not found or no access - fail explicitly
                return None, None, ''

        # Auto-select unique default by spec
        spec = request_data.get('spec', 'fury')
        class_name = SPEC_CLASS.get(spec.lower(), 'warrior')
        spec_key = f'{class_name}_{spec}'

        # Only consider global templates (owner_user_id=None) and user's own templates
        from django.db.models import Q
        templates = SimcContentTemplate.objects.filter(
            Q(owner_user_id=None) | Q(owner_user_id=self.user_id),
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec=spec_key,
            is_active=True
        )

        if templates.count() == 1:
            template = templates.first()
            return template.content, template.id, template.sync_version

        # Do not fall back to an unrelated specialization's template.
        # Zero or multiple candidates are explicit resolution failures.
        return None, None, ''

    def _render_template(self, template_content: str, request_data: Dict[str, Any]) -> str:
        """
        Render template with slot placeholders.

        All placeholders must be replaced.
        """
        result = template_content
        battlenet_actor_replaced = False
        if request_data.get('player_import_mode') == 'battlenet':
            # Legacy upstream base templates contain the actor-scoped options
            # immediately after a static actor. Replace that actor in place with
            # the armory actor; deleting it and inserting armory later would make
            # spec/race/consumables global options and SimC would ignore them.
            actor_pattern = '|'.join(
                ['warrior', 'mage', 'priest', 'paladin', 'druid', 'hunter',
                 'rogue', 'shaman', 'warlock', 'monk', 'demonhunter',
                 'demon_hunter', 'deathknight', 'death_knight', 'evoker']
            )
            armory_actor = self._get_slot_content('player_identity')
            result, replaced_count = re.subn(
                rf'(?mi)^\s*(?:{actor_pattern})\s*=.*$',
                armory_actor,
                result,
                count=1,
            )
            battlenet_actor_replaced = replaced_count == 1
            if battlenet_actor_replaced:
                # Armory is authoritative for player-scoped fields. Legacy base
                # templates may still carry a complete player from an old
                # expansion; retaining it would overwrite the imported actor.
                stale_player_keys = (
                    'spec', 'level', 'race', 'role', 'position', 'professions',
                    'talents', 'potion', 'flask', 'food', 'augmentation',
                    'temporary_enchant', 'gear_crit_rating', 'gear_haste_rating',
                    'gear_mastery_rating', 'gear_versatility_rating',
                )
                stale_pattern = '|'.join(re.escape(key) for key in stale_player_keys)
                result = re.sub(
                    rf'(?mi)^\s*(?:{stale_pattern})\s*=.*(?:\n|$)',
                    '',
                    result,
                )

        # Replace slot placeholders
        placeholders = {
            '{simulation_options}': self._get_slot_content('simulation_options'),
            '{player_identity}': (
                '' if battlenet_actor_replaced else self._get_slot_content('player_identity')
            ),
            '{equipment}': self._get_slot_content('equipment'),
            '{talents}': self._get_slot_content('talents'),
            '{stat_overrides}': self._get_slot_content('stat_overrides'),
            '{action_list}': self._get_slot_content('action_list'),
            '{output_options}': self._get_slot_content('output_options'),

            # Legacy placeholders for migration boundary
            '{player_config}': self._build_legacy_player_config(
                include_identity=not battlenet_actor_replaced,
            ),
            '{spec}': request_data.get('spec', ''),
            '{talent}': request_data.get('talent', ''),
            '{gear_crit}': str(request_data.get('gear_crit', 0)),
            '{gear_haste}': str(request_data.get('gear_haste', 0)),
            '{gear_mastery}': str(request_data.get('gear_mastery', 0)),
            '{gear_versatility}': str(request_data.get('gear_versatility', 0)),
            '{fight_style}': request_data.get('fight_style', 'Patchwerk'),
            '{time}': str(request_data.get('time', 300)),
            '{target_count}': str(request_data.get('target_count', 1)),
            '{result_file}': request_data.get('_result_file_path', 'result.html'),
        }

        for placeholder, value in placeholders.items():
            result = result.replace(placeholder, str(value))

        # Legacy templates may omit the output placeholder. The system-owned
        # output slot is mandatory, so append it exactly once when absent.
        output_content = self._get_slot_content('output_options')
        html_lines = [line for line in result.splitlines() if line.strip().startswith('html=')]
        if not html_lines and output_content:
            result = result.rstrip() + '\n' + output_content

        return result

    def _get_slot_content(self, slot_name: str) -> str:
        """Get content from resolved slot."""
        resolution = self.slots.get(slot_name)
        if not resolution or not resolution.value:
            return ''
        return resolution.value.content

    def _build_legacy_player_config(self, *, include_identity: bool = True) -> str:
        """Build legacy {player_config} for migration boundary."""
        parts = []

        identity = self._get_slot_content('player_identity')
        if include_identity and identity:
            parts.append(identity)

        equipment = self._get_slot_content('equipment')
        if equipment:
            parts.append(equipment)

        talents = self._get_slot_content('talents')
        if talents:
            parts.append(talents)

        stat_overrides = self._get_slot_content('stat_overrides')
        if stat_overrides:
            parts.append(stat_overrides)

        return '\n'.join(parts)

    def _count_actors(self, content: str) -> int:
        """Count actor definitions in final content."""
        actor_classes = ['warrior', 'mage', 'priest', 'paladin', 'druid', 'hunter',
                        'rogue', 'shaman', 'warlock', 'monk', 'demonhunter', 'demon_hunter',
                        'deathknight', 'death_knight', 'evoker']

        count = 0
        for line in content.split('\n'):
            line = line.strip()
            if '=' in line:
                key = line.split('=')[0].strip()
                if key in actor_classes or key == 'armory':
                    count += 1

        return count

    def _load_default_equipment(self, spec: str) -> Optional[SimcContentTemplate]:
        """Load default equipment template by spec with proper isolation."""
        spec = spec.lower()
        class_name = SPEC_CLASS.get(spec, 'warrior')
        spec_key = f'{class_name}_{spec}'

        # Only load global defaults (SOURCE_SIMC_UPSTREAM), user-private equipment not supported yet
        templates = SimcContentTemplate.objects.filter(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            spec=spec_key,
            is_active=True,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM
        )

        count = templates.count()
        if count == 0:
            return None
        elif count == 1:
            return templates.first()
        else:
            # Multiple defaults - fail explicitly
            raise ValueError(f'专精 {spec} 存在 {count} 个启用的默认装备模板，无法自动选择')

    def _has_actor_definition(self, content: str) -> bool:
        """Check if content already has an actor definition line."""
        actor_classes = ['warrior', 'mage', 'priest', 'paladin', 'druid', 'hunter',
                        'rogue', 'shaman', 'warlock', 'monk', 'demonhunter', 'demon_hunter',
                        'deathknight', 'death_knight', 'evoker']

        for line in content.split('\n'):
            line = line.strip()
            if '=' in line:
                key = line.split('=')[0].strip()
                if key in actor_classes:
                    return True
        return False

    def _find_unknown_placeholders(self, content: str) -> List[str]:
        """Find any {placeholder} that wasn't replaced."""
        import re
        placeholders = re.findall(r'\{([^}]+)\}', content)
        # Filter out valid non-placeholder braces (e.g., CSS, JSON)
        unknown = []
        for p in placeholders:
            # Skip if it looks like CSS/JSON syntax
            if ':' in p or ',' in p or p.isdigit():
                continue
            unknown.append('{' + p + '}')
        return unknown

    def _build_manifest(self):
        """Populate manifest from slot resolutions with full metadata."""
        from django.utils import timezone
        self.manifest.created_at = timezone.now().isoformat()
        self.manifest.user_id = self.user_id

        # Build full slot metadata
        for slot_name, resolution in self.slots.items():
            self.manifest.slots[slot_name] = resolution.to_manifest_entry()

        # Compute base template hash
        if hasattr(self, '_base_template_content'):
            self.manifest.base_template_hash = hashlib.sha256(
                self._base_template_content.encode('utf-8')
            ).hexdigest()

    def _parse_player_export(self, export_content: str) -> Dict[str, str]:
        """
        Parse addon/manual export into semantic slots.

        Returns dict with keys: identity, class, spec, talents, equipment, actions
        """
        lines = export_content.split('\n')

        # Extract identity (class="name" and spec=)
        identity_lines = []
        class_name = ''
        spec_name = ''
        talents_lines = []
        equipment_lines = []
        actions_lines = []

        actor_classes = ['warrior', 'mage', 'priest', 'paladin', 'druid', 'hunter',
                        'rogue', 'shaman', 'warlock', 'monk', 'demon_hunter',
                        'death_knight', 'evoker', 'demonhunter', 'deathknight']

        equipment_slots = ['head', 'neck', 'shoulder', 'back', 'chest', 'wrist',
                          'hands', 'waist', 'legs', 'feet', 'finger1', 'finger2',
                          'trinket1', 'trinket2', 'main_hand', 'off_hand']

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            # APL lines also contain '=', so classify them before generic
            # key/value parsing (actions=, actions+=, actions.foo=...).
            if stripped.startswith('actions'):
                actions_lines.append(stripped)
            # Check for actor definition
            elif '=' in stripped:
                key = stripped.split('=')[0].strip()

                if key in actor_classes:
                    identity_lines.append(stripped)
                    class_name = key
                elif key == 'spec':
                    identity_lines.append(stripped)
                    spec_name = stripped.split('=')[1].strip()
                elif key == 'level':
                    identity_lines.append(stripped)
                elif key == 'race':
                    identity_lines.append(stripped)
                elif key == 'role':
                    identity_lines.append(stripped)
                elif key == 'position':
                    identity_lines.append(stripped)
                elif key == 'professions':
                    identity_lines.append(stripped)
                elif key == 'talents':
                    talents_lines.append(stripped)
                elif key in equipment_slots:
                    equipment_lines.append(stripped)


        return {
            'identity': '\n'.join(identity_lines),
            'class': class_name,
            'spec': spec_name,
            'talents': '\n'.join(talents_lines),
            'equipment': '\n'.join(equipment_lines),
            'actions': '\n'.join(actions_lines),
        }

    @staticmethod
    def compute_input_hash(content: str) -> str:
        """Compute SHA256 hash of final_simc_content for deduplication."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
