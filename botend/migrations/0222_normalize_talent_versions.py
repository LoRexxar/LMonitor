from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


RETAIL_BUILD = '12.1.0.69283'
HISTORICAL_REFERENCE_IDS = (102435, 102436)


def normalize_talent_versions(apps, schema_editor):
    alias = schema_editor.connection.alias
    Version = apps.get_model('botend', 'WowTalentVersion')
    Node = apps.get_model('botend', 'WowTalentNodeMetadata')
    versions = Version.objects.using(alias)
    nodes = Node.objects.using(alias)

    old_retail = versions.filter(key='retail-12.0.7').first()
    legacy_current = versions.filter(key='ptr-12.1.0', branch='retail').first()
    retail = versions.filter(key='retail').first()

    if retail and legacy_current and retail.pk != legacy_current.pk:
        if nodes.filter(talent_version=retail).exists() and nodes.filter(talent_version=legacy_current).exists():
            raise RuntimeError('retail 与 ptr-12.1.0 均含天赋节点，无法自动判定权威数据桶')
        nodes.filter(talent_version=legacy_current).update(talent_version=retail)
        legacy_current.delete()
    elif not retail and legacy_current:
        legacy_current.key = 'retail'
        legacy_current.save(update_fields=['key'])
        retail = legacy_current
    elif not retail:
        retail = versions.create(key='retail')

    retail.label = '正式服 12.1.0'
    retail.branch = 'retail'
    retail.major_version = '12.1.0'
    if not retail.current_build:
        retail.current_build = RETAIL_BUILD
    if not retail.source_dir or 'ptr' in retail.source_dir.lower():
        retail.source_dir = '.cache/wago_db2_dumps/{}'.format(RETAIL_BUILD)
    retail.is_active = True
    retail.is_default_simulator = True
    retail.is_default_player_tree = True
    retail.is_default_stats = True
    retail.status = 'active'
    retail.activated_at = retail.activated_at or timezone.now()
    retail.notes = '正式服当前天赋数据；稳定 key 与版本号解耦。'
    retail.save()

    historical_rows = {}
    if old_retail:
        for identity in HISTORICAL_REFERENCE_IDS:
            row = nodes.filter(
                talent_version=old_retail,
                localization_only=False,
                node_id=identity,
            ).order_by('id').first()
            if row:
                historical_rows[identity] = row

    for identity, source in historical_rows.items():
        if nodes.filter(localization_only=True, name_kind='talent', reference_id=identity).exists():
            continue
        nodes.create(
            talent_version=retail,
            localization_only=True,
            name_kind='talent',
            reference_id=identity,
            reference_aliases=[],
            localization_evidence='历史攻略仍引用；从已下线的正式服 12.0.7 原生树转存。',
            class_name='',
            spec_name='',
            tree_type='spec',
            node_id=None,
            spell_id=source.spell_id,
            display_spell_id=source.display_spell_id,
            talent_id=source.talent_id,
            name=source.name,
            name_zh=source.name_zh,
            icon=source.icon,
            row=None,
            column=None,
            max_points=source.max_points,
            parents_json=[],
            description=source.description,
            description_zh=source.description_zh,
            source='historical_reference',
            last_updated=timezone.now(),
            db2_subtree_id=0,
            db2_tree_id=None,
            db2_component_id=0,
            flags=0,
        )

    if old_retail:
        old_retail.delete()

    ptr, _ = versions.get_or_create(key='ptr')
    ptr.label = 'PTR 12.1.5（待同步）'
    ptr.branch = 'ptr'
    ptr.major_version = '12.1.5'
    ptr.current_build = ''
    ptr.source_dir = '.cache/wago_db2_dumps/ptr'
    ptr.is_active = False
    ptr.is_default_simulator = False
    ptr.is_default_player_tree = False
    ptr.is_default_stats = False
    ptr.status = 'draft'
    ptr.activated_at = None
    ptr.notes = 'PTR 独立版本槽位；同步真实 12.1.5 DB2 后再启用。'
    ptr.save()

    versions.filter(key='ptr-12.1.0').update(
        is_active=False,
        is_default_simulator=False,
        is_default_player_tree=False,
        is_default_stats=False,
        status='retired',
        activated_at=None,
    )

    duplicates = list(
        nodes.filter(localization_only=True, reference_id__isnull=False)
        .values('name_kind', 'reference_id')
        .annotate(count=Count('id'))
        .filter(count__gt=1)
        .values_list('name_kind', 'reference_id')[:20]
    )
    if duplicates:
        raise RuntimeError('全局引用资料存在重复身份，需先人工归并：{}'.format(duplicates))


class Migration(migrations.Migration):
    dependencies = [('botend', '0221_expand_class_guide_tabs')]
    operations = [
        migrations.RunPython(normalize_talent_versions, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='wowtalentnodemetadata',
            name='wow_name_reference_unique',
        ),
        migrations.AddConstraint(
            model_name='wowtalentnodemetadata',
            constraint=models.UniqueConstraint(
                fields=('name_kind', 'reference_id'),
                name='wow_name_reference_unique',
            ),
        ),
    ]
