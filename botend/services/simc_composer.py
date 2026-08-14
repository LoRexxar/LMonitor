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
from botend.models import SimcContentTemplate, SimcApl, SimcProfile
from botend.services.simc_player_config import (
    SPEC_CLASS,
    canonical_simc_profile_identity,
    canonical_simc_spec_identity,
)


# Server-owned SimulationCraft raid-buff contract. Persisted values are bare
# keys; rendering is the only place that adds the executable ``override.``
# prefix. Keep this order stable for snapshots and UI presentation.
SIMC_RAID_BUFF_VALUES = (
    'arcane_intellect',
    'battle_shout',
    'mark_of_the_wild',
    'power_word_fortitude',
    'skyfury',
    'chaos_brand',
    'mystic_touch',
    'hunters_mark',
    'mortal_wounds',
    'bleeding',
    'bloodlust',
)

SIMC_EXTRA_OPTIONS = (
    {
        'value': 'power_infusion',
        'label': '牧师能量灌注',
        'description': '从开战起按 120 秒间隔定时施加能量灌注。',
        'simc_external_buff': 'power_infusion',
        'cooldown_seconds': 120,
    },
)
SIMC_EXTRA_OPTION_VALUES = frozenset(option['value'] for option in SIMC_EXTRA_OPTIONS)
SIMC_EXTRA_OPTION_BY_VALUE = {
    option['value']: option for option in SIMC_EXTRA_OPTIONS
}


def render_simc_extra_option_lines(
        value: str, request_data: Dict[str, Any]) -> tuple[str, ...]:
    """Render version-stable SimC input for a validated extra option."""
    option = SIMC_EXTRA_OPTION_BY_VALUE[value]
    buff_name = option['simc_external_buff']
    cooldown = option['cooldown_seconds']
    max_time = float(request_data.get('time', request_data.get('max_time', 300)))
    timings = '/'.join(
        str(second) for second in range(0, max(1, math.ceil(max_time)), cooldown)
    )
    return (f'external_buffs.{buff_name}={timings}',)

# Implicit defaults are limited to raid effects supplied by the actor's own
# class. ``use_class_raid_buff`` allows those defaults to be unioned with the
# explicitly selected extra ``raid_buffs``. Historical requests without the
# toggle retain the old three-state contract.
SIMC_CLASS_RAID_BUFFS = {
    'mage': ('arcane_intellect',),
    'warrior': ('battle_shout',),
    'druid': ('mark_of_the_wild',),
    'priest': ('power_word_fortitude',),
    'shaman': ('skyfury',),
    'demonhunter': ('chaos_brand',),
    'monk': ('mystic_touch',),
    'hunter': ('hunters_mark',),
}


def default_raid_buffs_for_actor(request_data: Dict[str, Any]) -> tuple[str, ...]:
    class_name = str(request_data.get('_trusted_class_name') or '').strip().lower()
    spec = str(request_data.get('spec') or '').strip().lower()
    if not class_name:
        if spec in SPEC_CLASS:
            class_name = SPEC_CLASS[spec]
        elif '_' in spec:
            class_name = spec.split('_', 1)[0]
    return SIMC_CLASS_RAID_BUFFS.get(class_name, ())


def validate_simulation_options(params: Dict[str, Any]) -> str:
    """Validate canonical persisted options, with request-name compatibility."""
    def value(canonical_name, request_name, default):
        return params[canonical_name] if canonical_name in params else params.get(request_name, default)

    def integer(name, item, minimum, maximum):
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            return f'{name} 必须是 {minimum} 到 {maximum} 之间的整数'
        return ''

    def bounded_number(name, item, minimum, maximum):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return f'{name} 必须是数字'
        item = float(item)
        if not math.isfinite(item) or not minimum <= item <= maximum:
            return f'{name} 必须在 {minimum} 到 {maximum} 之间'
        return ''

    def number(name, minimum, maximum):
        item = params.get(name)
        if item is None:
            return ''
        return bounded_number(name, item, minimum, maximum)

    errors = [
        integer('iterations', params.get('iterations', 10000), 1, 100000000),
        bounded_number('max_time', value('max_time', 'time', 300), 1, 86400),
        integer('desired_targets', value('desired_targets', 'target_count', 1), 1, 1000),
        number('target_error', 0, 1),
        number('vary_combat_length', 0, 1),
    ]
    # If callers submit both schemas, also validate the request-side value that
    # Composer will render; canonical aliases must never mask an unsafe value.
    if 'max_time' in params and 'time' in params:
        errors.append(bounded_number('time', params['time'], 1, 86400))
    if 'desired_targets' in params and 'target_count' in params:
        errors.append(integer('target_count', params['target_count'], 1, 1000))
    if 'use_class_raid_buff' in params and not isinstance(params['use_class_raid_buff'], bool):
        errors.append('use_class_raid_buff 必须是布尔值')
    if 'raid_buffs' in params:
        raid_buffs = params['raid_buffs']
        if not isinstance(raid_buffs, list):
            errors.append('raid_buffs 必须是列表')
        elif any(not isinstance(item, str) for item in raid_buffs):
            errors.append('raid_buffs 只能包含字符串')
        elif len(set(raid_buffs)) != len(raid_buffs):
            errors.append('raid_buffs 不允许重复值')
        elif any(item not in SIMC_RAID_BUFF_VALUES for item in raid_buffs):
            errors.append('raid_buffs 包含不支持的 Raid Buff')
    if 'extra_options' in params:
        extra_options = params['extra_options']
        if not isinstance(extra_options, list):
            errors.append('extra_options 必须是列表')
        elif any(not isinstance(item, str) for item in extra_options):
            errors.append('extra_options 只能包含字符串')
        elif len(set(extra_options)) != len(extra_options):
            errors.append('extra_options 不允许重复值')
        elif any(item not in SIMC_EXTRA_OPTION_VALUES for item in extra_options):
            errors.append('extra_options 包含不支持的额外选项')
    if 'profile_overrides' in params:
        overrides = params['profile_overrides']
        allowed = {
            'flask', 'potion', 'food', 'augmentation', 'temporary_enchant',
            'talents', 'class_talents', 'spec_talents', 'hero_talents',
        }
        if not isinstance(overrides, dict):
            errors.append('profile_overrides 必须是对象')
        elif any(key not in allowed for key in overrides):
            errors.append('profile_overrides 包含不支持的字段')
        elif any(
            not isinstance(item, str) or not item.strip()
            or '\n' in item or '\r' in item or '=' in item
            for item in overrides.values()
        ):
            errors.append('profile_overrides 覆盖值格式无效')
    for error in errors:
        if error:
            return error
    for name, default in (('fight_style', 'Patchwerk'), ('enemy_type', '')):
        item = params.get(name, default)
        if item is None and name == 'enemy_type':
            item = ''
        if not isinstance(item, str) or (item and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', item)):
            return f'{name} 包含无效值'
    return ''


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
    user_id: Optional[int] = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2)


class SimcComposer:
    """Compose frozen SimC input from semantic slot resolution."""

    def __init__(self, user_id: Optional[int]):
        self.user_id = user_id
        self.slots: Dict[str, SlotResolution] = {}
        self.manifest = CompositionManifest()

    def compose_validation_input(self, profile, apl_source: str) -> str:
        """Render a complete validation document through the normal semantic slots.

        The template is service-owned so validation cannot select or execute mutable
        user template text.  Profile/APL values still pass through the same slot
        arbitration used by real runs.
        """
        validation_template = (
            '{simulation_options}\n{player_identity}\n{talents}\n{equipment}\n'
            '{stat_overrides}\n{action_list}\n{output_options}'
        )
        _, validation_spec = canonical_simc_profile_identity(profile.spec, getattr(profile, 'class_name', ''))
        request = {
            'spec': validation_spec or profile.spec,
            'use_ptr': bool(getattr(profile, 'use_ptr', False)),
            'player_import_mode': profile.player_config_mode,
            # Validation profiles are server-owned.  Carry their canonical class
            # separately so ambiguous short specs (frost/protection/holy/etc.) do
            # not fall back to SPEC_CLASS's legacy single-class interpretation.
            '_trusted_class_name': getattr(profile, 'class_name', ''),
            'player_equipment': profile.player_equipment, 'talent': profile.talent,
            'battlenet_region': profile.battlenet_region,
            'battlenet_realm': profile.battlenet_realm,
            'battlenet_character': profile.battlenet_character,
            'gear_strength': getattr(profile, 'gear_strength', None),
            'gear_crit': profile.gear_crit, 'gear_haste': profile.gear_haste,
            'gear_mastery': profile.gear_mastery, 'gear_versatility': profile.gear_versatility,
            'override_action_list': apl_source,
            'base_template_content': validation_template,
            'fight_style': 'Patchwerk', 'time': 1, 'target_count': 1,
            'iterations': 1, 'vary_combat_length': 0,
            '_result_file_path': 'validation-result.html',
        }
        content, _, error = self.compose(request)
        if error or content is None:
            raise ValueError('Could not compose validation input.')
        return content

    @classmethod
    def validation_context(cls, profile, *, catalog_revision='', binary_revision='',
                           validation_input=None):
        return {
            'profile_id': profile.id,
            'user_id': profile.user_id,
            'spec': profile.spec,
            'player_import_mode': profile.player_config_mode,
            'catalog_revision': catalog_revision,
            'binary_revision': binary_revision,
            'validation_input': validation_input,
        }

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

        additional_input = str(request_data.get('additional_simc_input') or '').strip()
        self.slots['additional_simc_input'] = SlotResolution(
            slot_name='additional_simc_input',
            value=SlotValue(
                content=additional_input,
                source='task_request',
                content_hash=hashlib.sha256(additional_input.encode('utf-8')).hexdigest(),
            ),
            status='resolved' if additional_input else 'missing',
        )

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
        canonical_class, canonical_spec = canonical_simc_spec_identity(user_spec)
        player_import_mode = request_data.get('player_import_mode', '').strip()

        # Dashboard resources use class-qualified keys (for example
        # ``warrior_arms``), while executable SimC actor blocks use ``arms``.
        # Compare and render the canonical SimC identity, not the storage key.
        trusted_class = str(request_data.get('_trusted_class_name') or '').strip().lower()
        trusted_canonical_class, _ = canonical_simc_spec_identity(trusted_class)
        derived_class = (
            trusted_canonical_class or trusted_class or canonical_class
            or (SPEC_CLASS.get(user_spec) if user_spec else None)
        )
        comparable_spec = canonical_spec or user_spec

        # For battlenet mode, prefer the frozen actor block. The Battle.net identity
        # is source metadata and is only an execution fallback for legacy rows.
        if player_import_mode == 'battlenet':
            player_equipment = (request_data.get('player_equipment') or '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                export_spec = parsed.get('spec', '').strip().lower()
                export_class = parsed.get('class', '').strip().lower()
                if comparable_spec and export_spec and comparable_spec != export_spec:
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
            if comparable_spec and bnet_spec and comparable_spec != bnet_spec:
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

        # Frozen actor-export modes parse the baseline identity and actor-scoped
        # options as well as equipment. Attribute-only baselines must follow the
        # same path or valid consumables are silently dropped from composed input.
        if player_import_mode in (
                'addon_full_export', 'manual_equipment', 'wcl', 'attribute_only'):
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                export_spec = parsed.get('spec', '').strip().lower()
                export_class = parsed.get('class', '').strip().lower()

                # Check spec conflict
                if comparable_spec and export_spec and comparable_spec != export_spec:
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
                            source={
                                'addon_full_export': 'addon_export',
                                'manual_equipment': 'manual_equipment',
                                'wcl': 'wcl_export',
                                'attribute_only': 'attribute_frozen_baseline',
                            }[player_import_mode],
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
        final_spec = comparable_spec or 'fury'
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

        # WCL profiles are frozen full actor exports, with the same semantic
        # split as addon exports. They must not fall back to mutable defaults.
        if player_import_mode == 'wcl' and player_equipment:
            parsed = self._parse_player_export(player_equipment)
            equipment_content = parsed['equipment']
            content_hash = hashlib.sha256(equipment_content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='equipment',
                value=SlotValue(content=equipment_content, source='wcl_export', content_hash=content_hash),
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
                content_hash = hashlib.sha256(default_equipment.player_equipment.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=default_equipment.player_equipment,
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
                content_hash = hashlib.sha256(default_equipment.player_equipment.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='equipment',
                    value=SlotValue(
                        content=default_equipment.player_equipment,
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

        talent = (request_data.get('talent') or '').strip()

        # Saved profiles persist their canonical build code separately. Prefer it
        # over any stale/exporter-specific talents line retained in the actor block,
        # so every profile mode renders exactly one authoritative talent input.
        # Omnium selections are a separate SimC directive, however: they are not
        # encoded in the canonical build code and must survive that replacement.
        if talent:
            lines = [f'talents={talent}']
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                lines.extend(
                    line for line in parsed['talents'].splitlines()
                    if line.split('=', 1)[0].strip() == 'omnium_talents'
                )
            content = '\n'.join(lines)
            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            return SlotResolution(
                slot_name='talents',
                value=SlotValue(content=content, source='user_input', content_hash=content_hash),
                status='resolved'
            )

        # Frozen player exports own their talents; the workbench APL remains separate.
        if player_import_mode in ('addon_full_export', 'manual_equipment', 'battlenet', 'wcl'):
            player_equipment = request_data.get('player_equipment', '').strip()
            if player_equipment:
                parsed = self._parse_player_export(player_equipment)
                if parsed['talents']:
                    content_hash = hashlib.sha256(parsed['talents'].encode('utf-8')).hexdigest()
                    source = {
                        'addon_full_export': 'addon_export',
                        'manual_equipment': 'manual_equipment',
                        'battlenet': 'battlenet_snapshot',
                        'wcl': 'wcl_export',
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

        # A task's frozen APL is authoritative, including an explicit empty value.
        # Resolve it before parsing embedded actions from an imported Profile.
        if override_apl is not None:
            if override_apl == '':
                content_hash = hashlib.sha256(b'').hexdigest()
                return SlotResolution(
                    slot_name='action_list',
                    value=SlotValue(content='', source='user_explicit_empty', content_hash=content_hash),
                    status='explicit_empty'
                )
            if override_apl:
                content_hash = hashlib.sha256(override_apl.encode('utf-8')).hexdigest()
                return SlotResolution(
                    slot_name='action_list',
                    value=SlotValue(content=override_apl, source='user_override', content_hash=content_hash),
                    status='resolved'
                )

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
        ]
        if 'use_class_raid_buff' in request_data:
            selected_raid_buffs = set(request_data.get('raid_buffs') or ())
            if request_data.get('use_class_raid_buff') is True:
                selected_raid_buffs = selected_raid_buffs.union(
                    default_raid_buffs_for_actor(request_data)
                )
            options.extend(
                f'override.{name}={1 if name in selected_raid_buffs else 0}'
                for name in SIMC_RAID_BUFF_VALUES
            )
        elif 'raid_buffs' in request_data:
            selected_raid_buffs = set(request_data['raid_buffs'])
            options.extend(
                f'override.{name}={1 if name in selected_raid_buffs else 0}'
                for name in SIMC_RAID_BUFF_VALUES
            )
        else:
            selected_raid_buffs = set(default_raid_buffs_for_actor(request_data))
            options.extend(
                f'override.{name}={1 if name in selected_raid_buffs else 0}'
                for name in SIMC_RAID_BUFF_VALUES
            )
        options.append(f"iterations={request_data.get('iterations', 10000)}")
        # PTR is frozen with the Profile resource. Missing keys on historical
        # versions remain Live; only the explicit boolean true enables PTR.
        if request_data.get('use_ptr') is True:
            options.insert(0, 'ptr=1')
        if request_data.get('target_error') is not None:
            options.append(f"target_error={request_data['target_error']}")
        if request_data.get('vary_combat_length') is not None:
            options.append(f"vary_combat_length={request_data['vary_combat_length']}")
        if request_data.get('enemy_type'):
            options.append(f"enemy={request_data['enemy_type']}")
        candidate_options = request_data.get('_candidate_simc_options')
        if candidate_options is not None:
            from botend.services.simc_candidate_options import normalize_controlled_simc_options
            controlled_options = normalize_controlled_simc_options(
                candidate_options, allow_absent=False,
            )
            options.extend(controlled_options or [])
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
        """Compatibility entry point; policy lives in the public validator."""
        return validate_simulation_options(request_data)

    def _resolve_stat_overrides(self, request_data: Dict[str, Any]) -> SlotResolution:
        """Resolve actor-scoped options for every player source."""
        overrides = []
        for field in ('strength', 'crit', 'haste', 'mastery', 'versatility'):
            value = request_data.get(f'gear_{field}')
            if value is not None:
                suffix = '' if field == 'strength' else '_rating'
                overrides.append(f'gear_{field}{suffix}={value}')
        for extra_option in request_data.get('extra_options') or ():
            overrides.extend(render_simc_extra_option_lines(extra_option, request_data))

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

    def _load_template_with_access_check(self, template_id: int) -> Optional[SimcContentTemplate]:
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
                is_active=True,
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
            template = self._load_template_with_access_check(base_template_id)
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
        player_import_mode = request_data.get('player_import_mode')
        # Stat values are owned by the explicit override slot for every source.
        # Remove legacy template totals first so each requested override is emitted
        # once, while omitted values continue to come from the actual equipment.
        result = re.sub(
            r'(?mi)^\s*gear_(?:strength|crit|haste|mastery|versatility)(?:_rating)?\s*=.*(?:\n|$)',
            '',
            result,
        )
        if self._get_slot_content('talents'):
            result = re.sub(r'(?mi)^\s*talents\s*=.*(?:\n|$)', '', result)
        battlenet_actor_replaced = False
        if player_import_mode == 'battlenet':
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
                    'temporary_enchant', 'gear_strength',
                    'gear_crit_rating', 'gear_haste_rating',
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
            '{additional_simc_input}': self._get_slot_content('additional_simc_input'),
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
            '{iterations}': str(request_data.get('iterations', 10000)),
            '{result_file}': request_data.get('_result_file_path', 'result.html'),
        }

        for placeholder, value in placeholders.items():
            result = result.replace(placeholder, str(value))

        additional_content = self._get_slot_content('additional_simc_input')
        if '{additional_simc_input}' not in template_content and additional_content:
            player_markers = [
                self._get_slot_content('equipment'),
                self._get_slot_content('talents'),
                self._get_slot_content('player_identity'),
            ]
            insertion_at = -1
            for marker in player_markers:
                if marker and marker in result:
                    insertion_at = max(insertion_at, result.index(marker) + len(marker))
            if insertion_at >= 0:
                result = (
                    result[:insertion_at].rstrip() + '\n' + additional_content + '\n'
                    + result[insertion_at:].lstrip()
                )
            else:
                result = result.rstrip() + '\n' + additional_content

        # A selected/overridden APL is an authoritative semantic slot, not an
        # optional decoration of the base template. Older templates commonly
        # predate ``{action_list}``; append the resolved slot so local and remote
        # execution cannot silently omit the frozen APL or candidate override.
        action_content = self._get_slot_content('action_list')
        if '{action_list}' not in template_content and action_content:
            result = result.rstrip() + '\n' + action_content

        # Legacy templates may omit the output placeholder. The system-owned
        # output slot is mandatory, so append it exactly once when absent.
        output_content = self._get_slot_content('output_options')
        html_lines = [line for line in result.splitlines() if line.strip().startswith('html=')]
        if not html_lines and output_content:
            result = result.rstrip() + '\n' + output_content

        # Task-scoped Profile overrides are applied after all source/template
        # arbitration, so they work consistently for imported and armory actors.
        result = self._apply_profile_overrides(result, request_data.get('profile_overrides') or {})
        return result

    def _apply_profile_overrides(self, content: str, overrides: Dict[str, Any]) -> str:
        """Replace only explicitly supplied actor-scoped Profile fields."""
        allowed = {
            'flask', 'potion', 'food', 'augmentation', 'temporary_enchant',
            'talents', 'class_talents', 'spec_talents', 'hero_talents',
        }
        for key, raw_value in overrides.items():
            if key not in allowed or not isinstance(raw_value, str):
                continue
            value = raw_value.strip()
            if not value or '\n' in value or '\r' in value:
                continue
            line = f'{key}={value}'
            content, count = re.subn(
                rf'(?mi)^\s*{re.escape(key)}\s*=.*(?:\n|$)',
                line + '\n', content,
            )
            if count == 0:
                content = content.rstrip() + '\n' + line
        return content.rstrip() + '\n'

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

    def _load_default_equipment(self, spec: str) -> Optional[SimcProfile]:
        """Load default equipment template by spec with proper isolation."""
        spec = spec.lower()
        class_name = SPEC_CLASS.get(spec, 'warrior')
        spec_key = f'{class_name}_{spec}'

        # Only load global defaults (SOURCE_SIMC_UPSTREAM), user-private equipment not supported yet
        templates = SimcProfile.objects.filter(
            user_id__isnull=True,
            spec=spec_key,
            is_active=True,
            source=SimcProfile.SOURCE_SIMC_UPSTREAM,
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
                          'trinket1', 'trinket2', 'main_hand', 'off_hand', 'tabard']

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            # APL lines also contain '=', so classify them before generic
            # Action directives own the action-list slot. This includes SimC's
            # default_actions switch: retaining it beside a frozen APL makes SimC
            # discard the selected actions and regenerate its built-in rotation.
            if stripped.startswith('actions') or stripped.startswith('default_actions='):
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
                elif key in ('professions', 'region', 'server', 'loot_spec'):
                    identity_lines.append(stripped)
                elif key in (
                    'talents', 'talent', 'omnium_talents',
                    'class_talents', 'spec_talents', 'hero_talents',
                ):
                    # SimC only recognizes the canonical plural build directive.
                    # Normalize legacy imported profiles instead of silently running
                    # the class default build under an ignored `talent=` line.
                    if key == 'talent':
                        stripped = f"talents={stripped.split('=', 1)[1].strip()}"
                    # Hero-tree selections are part of the talent slot and must
                    # remain coupled to the class/spec build code.
                    talents_lines.append(stripped)
                elif key == 'ptr':
                    # PTR selection is owned by the frozen profile execution
                    # property and emitted once in global simulation options.
                    continue
                elif re.fullmatch(
                    r'gear_(?:strength|crit|haste|mastery|versatility)(?:_rating)?',
                    key,
                    flags=re.IGNORECASE,
                ):
                    # Imported actor blocks may contain historical stat overrides.
                    # The persisted Profile fields are the sole executable override
                    # contract, so never let an imported value bypass that layer.
                    continue
                elif key in equipment_slots:
                    equipment_lines.append(stripped)
                else:
                    # A SimC export is an actor profile, not a closed schema.
                    # Preserve unknown actor-scoped options instead of silently
                    # dropping valid fields added by a newer SimC/exporter.
                    identity_lines.append(stripped)

        # A canonical build code already contains class, specialization and hero
        # selections.  Do not submit the exporter's redundant split node lists to
        # SimC as well: current PTR builds reject duplicate/stale spell entries.
        # Historical exports without a build code retain their legacy directives.
        if any(line.split('=', 1)[0].strip() == 'talents' for line in talents_lines):
            talents_lines = [
                line for line in talents_lines
                if line.split('=', 1)[0].strip() not in {
                    'class_talents', 'spec_talents', 'hero_talents',
                }
            ]

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
