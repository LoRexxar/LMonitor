"""手册测试显式模拟后台预热，文件与真实运行目录隔离。"""
from io import StringIO
from pathlib import Path
import tempfile
from django.core.management import call_command
from django.test import override_settings


def isolate_journal_snapshots(test):
    directory = tempfile.TemporaryDirectory(prefix='journal-loot-test-')
    test.addCleanup(directory.cleanup)
    root = Path(directory.name)
    override = override_settings(JOURNAL_LOOT_SNAPSHOT_ROOT=root / 'journal',
                                 JOURNAL_SNAPSHOT_ROOT=root / 'catalog', GEAR_CATALOG_SNAPSHOT_ROOT=root / 'gear')
    override.enable()
    test.addCleanup(override.disable)
    return root


def seed_snapshot_difficulty_facts(release):
    """Explicit unit-only complete profile; never called by ordinary warm_journal.

    Numbers below are synthetic fixture facts, not production evidence. The real
    catalog reader still has to match build, instance, difficulty, context/bonus.
    """
    from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
    from botend.portal.adventure_journal import instance_source, difficulty_item_context
    from django.db.models import Exists, OuterRef
    # Match the canonical catalog reader without changing business season state.
    active = SeasonMeta.objects.filter(is_active=True).exclude(gear_batch_key='').annotate(
        has_gear_catalog=Exists(WowItemVariantSnapshot.objects.filter(
            season_id=OuterRef('pk'), batch_key=OuterRef('gear_batch_key'))))
    season = (active.filter(has_gear_catalog=True).order_by('-gear_synced_at', '-id').first()
              or active.order_by('-id').first())
    if season is None:
        season, _ = SeasonMeta.objects.get_or_create(season_key='snapshot-unit', defaults={
            'season_name': 'Snapshot unit', 'is_active': True, 'gear_batch_key': 'snapshot-unit-batch',
            'mplus_zone_id': 1, 'raid_zone_id': 2})
    for instance in release.instances.all():
        source = instance_source(release, instance.journal_id, season=season)
        branch = source.get('data_branch') or ('ptr' if source['key'] == 'ptr' else 'retail')
        for boss in instance.encounters.all():
            for drop in boss.payload['loot']:
                item, _ = WowItemSnapshot.objects.get_or_create(item_id=drop['item_id'], defaults={
                    'name': drop['name'], 'name_zh': '', 'quality': drop['quality'],
                    'catalog_type': 'equipment' if drop['class_id'] in (2, 4) else 'misc',
                    'inventory_type': drop['slot']})
                if item.catalog_type != 'equipment':
                    continue
                for difficulty in drop['difficulty_ids']:
                    context = difficulty_item_context(release.manifest['catalog']['difficulties'], difficulty)
                    bonus = 100000 + difficulty
                    WowItemVariantSnapshot.objects.update_or_create(item=item, season=season,
                        batch_key=season.gear_batch_key,
                        variant_key=f"unit-{instance.journal_id}-{difficulty}-{item.item_id}", defaults={
                            'variant_type': 'drop_equipment', 'data_branch': branch,
                            'item_level': 200 + difficulty * 10, 'bonus_ids': [bonus],
                            'stats_json': {'intellect': 10 + difficulty}, 'effects_json': ['装备：合成测试效果。'],
                            'metadata': {'difficulty_drop': {
                                'schema': 1, 'provider': 'wago', 'difficulty_id': difficulty,
                                'item_context': context, 'instance_id': instance.journal_id,
                                'rule_build': source['build'], 'quality': drop['quality'], 'bonus_ids': [bonus]},
                                'tooltip_source': {'provider': 'wowhead', 'game_build': '',
                                    'url': f'https://nether.wowhead.com/tooltip/item/{item.item_id}?bonus={bonus}'}}})
    return season


def warm_journal(instance=None, *, complete_profile=False):
    if complete_profile:
        from botend.portal.adventure_journal import current_release
        seed_snapshot_difficulty_facts(current_release())
    call_command('refresh_journal_snapshot', stdout=StringIO())
    call_command('refresh_journal_loot_snapshots', force=True, instance=instance, stdout=StringIO())
