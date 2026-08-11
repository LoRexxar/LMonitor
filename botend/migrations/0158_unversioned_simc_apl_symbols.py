# 手工生成：保留并规范化现有 APL 字段数据。

import re
from collections import defaultdict

import django.db.models.deletion
from django.db import migrations, models


SCOPE_FIELDS = (
    'class_name', 'spec', 'hero_tree', 'class_key', 'spec_key', 'hero_tree_key',
    'spell_id', 'trait_id', 'source', 'identity_source', 'identity_reason',
    'identity_candidates', 'aliases', 'options', 'name_en', 'name_zh',
    'localization_source', 'localization_status', 'metadata', 'is_active',
)


def _normalized_scope(row):
    values = []
    for field in ('class_name', 'spec', 'hero_tree'):
        value = str(getattr(row, field, '') or '').strip().lower()
        values.append(value)
    return tuple(values)


def _information_score(row):
    return sum(bool(getattr(row, field, None)) for field in (
        'spell_id', 'trait_id', 'name_zh', 'name_en', 'identity_source',
        'identity_candidates', 'aliases', 'options', 'metadata',
    ))


def _preference(row):
    return (
        1 if row.source == 'manual' else 0,
        1 if row.is_active else 0,
        _information_score(row),
        row.updated_at,
        row.id,
    )


def _has_value(value):
    return value not in (None, '', [], {})


def migrate_symbols(apps, schema_editor):
    """把版本化重复行折叠为字段主体，并完整迁移职业/专精归属。"""
    Symbol = apps.get_model('botend', 'SimcAplSymbol')
    Scope = apps.get_model('botend', 'SimcAplSymbolScope')
    Backend = apps.get_model('botend', 'SimcBackendBinary')

    rows = list(Symbol.objects.all().order_by('id'))
    active_builds_by_revision = defaultdict(set)
    for row in rows:
        if row.is_active and row.simc_revision and row.wow_build:
            active_builds_by_revision[str(row.simc_revision)].add(str(row.wow_build))

    for backend in Backend.objects.all():
        current = str(backend.current_version or '').strip().lower()
        revisions = []
        if re.fullmatch(r'[0-9a-f]{40}', current):
            revisions = [current]
        else:
            match = re.search(r'(?:^|-)([0-9a-f]{7,39})$', current)
            if match:
                revisions = [
                    revision for revision in active_builds_by_revision
                    if revision.startswith(match.group(1))
                ]
        builds = {
            build for revision in revisions
            for build in active_builds_by_revision.get(revision, ())
        }
        if len(builds) == 1:
            backend.game_build = next(iter(builds))
            backend.save(update_fields=['game_build'])

    symbol_groups = defaultdict(list)
    for row in rows:
        identity = (
            str(row.token or '').strip().lower(),
            str(row.symbol_kind or 'action').strip().lower(),
        )
        symbol_groups[identity].append(row)

    for (token, kind), group in symbol_groups.items():
        keeper = max(group, key=_preference)
        scope_groups = defaultdict(list)
        for row in group:
            scope_groups[_normalized_scope(row)].append(row)

        active_scope_found = False
        for (class_key, spec_key, hero_tree_key), scope_group in scope_groups.items():
            winner = max(scope_group, key=_preference)
            values = {field: getattr(winner, field) for field in SCOPE_FIELDS}
            for field in SCOPE_FIELDS:
                if _has_value(values[field]):
                    continue
                for candidate in sorted(scope_group, key=_preference, reverse=True):
                    candidate_value = getattr(candidate, field)
                    if _has_value(candidate_value):
                        values[field] = candidate_value
                        break
            values.update(
                symbol_id=keeper.id,
                class_name=class_key or None,
                spec=spec_key or None,
                hero_tree=hero_tree_key or None,
                class_key=class_key,
                spec_key=spec_key,
                hero_tree_key=hero_tree_key,
                is_active=any(row.is_active for row in scope_group),
            )
            active_scope_found = active_scope_found or values['is_active']
            Scope.objects.create(**values)

        duplicate_ids = [row.id for row in group if row.id != keeper.id]
        if duplicate_ids:
            Symbol.objects.filter(pk__in=duplicate_ids).delete()
        update_fields = []
        if keeper.token != token:
            keeper.token = token
            update_fields.append('token')
        if keeper.symbol_kind != kind:
            keeper.symbol_kind = kind
            update_fields.append('symbol_kind')
        if keeper.is_active != active_scope_found:
            keeper.is_active = active_scope_found
            update_fields.append('is_active')
        if update_fields:
            keeper.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0157_add_simc_builtin_apl_source'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimcAplSymbolScope',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('class_name', models.CharField(blank=True, max_length=50, null=True)),
                ('spec', models.CharField(blank=True, max_length=100, null=True)),
                ('hero_tree', models.CharField(blank=True, max_length=100, null=True)),
                ('class_key', models.CharField(default='', editable=False, max_length=50)),
                ('spec_key', models.CharField(default='', editable=False, max_length=100)),
                ('hero_tree_key', models.CharField(default='', editable=False, max_length=100)),
                ('spell_id', models.BigIntegerField(blank=True, null=True)),
                ('trait_id', models.BigIntegerField(blank=True, null=True)),
                ('source', models.CharField(choices=[('simc_manifest', 'SimC manifest'), ('system_apl', 'System APL'), ('wago', 'Wago'), ('manual', 'Verified manual fact')], default='simc_manifest', max_length=32)),
                ('identity_source', models.CharField(blank=True, default='', max_length=64)),
                ('identity_reason', models.CharField(blank=True, default='', max_length=128)),
                ('identity_candidates', models.JSONField(blank=True, default=list)),
                ('aliases', models.JSONField(blank=True, default=list)),
                ('options', models.JSONField(blank=True, default=dict)),
                ('name_en', models.CharField(blank=True, default='', help_text='当前归属下的 APL 英文名称；至少保留原始 token', max_length=255)),
                ('name_zh', models.CharField(blank=True, default='', help_text='当前归属下的 APL 简体中文名称', max_length=255)),
                ('localization_source', models.CharField(blank=True, default='', max_length=64)),
                ('localization_status', models.CharField(blank=True, default='', max_length=32)),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='当前职业/专精下的表达式模板、Wowhead 与覆盖审计元数据')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('symbol', models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='scopes', to='botend.simcaplsymbol')),
            ],
            options={
                'verbose_name': 'SimC APL field scope',
                'verbose_name_plural': 'SimC APL field scopes',
                'db_table': 'simc_apl_symbol_scope',
                'ordering': ['symbol__symbol_kind', 'symbol__token', 'class_key', 'spec_key', 'id'],
            },
        ),
        migrations.AddField(
            model_name='simcbackendbinary',
            name='game_build',
            field=models.CharField(blank=True, default='', help_text='该后端二进制对应的 WoW 数据版本；不参与 APL 字段去重', max_length=64),
        ),
        migrations.RunPython(migrate_symbols, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='simcaplsymbol',
            options={'ordering': ['symbol_kind', 'token', 'id'], 'verbose_name': 'SimC APL field', 'verbose_name_plural': 'SimC APL fields'},
        ),
        migrations.RemoveConstraint(model_name='simcaplsymbol', name='simc_symbol_version_scope_uniq'),
        migrations.RemoveConstraint(model_name='simcaplsymbol', name='simc_symbol_class_scope_key_ck'),
        migrations.RemoveConstraint(model_name='simcaplsymbol', name='simc_symbol_spec_scope_key_ck'),
        migrations.RemoveConstraint(model_name='simcaplsymbol', name='simc_symbol_hero_scope_key_ck'),
        migrations.RemoveIndex(model_name='simcaplsymbol', name='simc_sym_rev_spec_kind_tok_idx'),
        migrations.RemoveIndex(model_name='simcaplsymbol', name='simc_sym_rev_spell_idx'),
        migrations.RemoveIndex(model_name='simcaplsymbol', name='simc_sym_rev_class_hero_idx'),
        migrations.RemoveIndex(model_name='simcaplsymbol', name='simc_sym_rev_trait_idx'),
        migrations.RemoveField(model_name='simcaplsymbol', name='aliases'),
        migrations.RemoveField(model_name='simcaplsymbol', name='class_key'),
        migrations.RemoveField(model_name='simcaplsymbol', name='class_name'),
        migrations.RemoveField(model_name='simcaplsymbol', name='hero_tree'),
        migrations.RemoveField(model_name='simcaplsymbol', name='hero_tree_key'),
        migrations.RemoveField(model_name='simcaplsymbol', name='identity_candidates'),
        migrations.RemoveField(model_name='simcaplsymbol', name='identity_reason'),
        migrations.RemoveField(model_name='simcaplsymbol', name='identity_source'),
        migrations.RemoveField(model_name='simcaplsymbol', name='localization_source'),
        migrations.RemoveField(model_name='simcaplsymbol', name='localization_status'),
        migrations.RemoveField(model_name='simcaplsymbol', name='metadata'),
        migrations.RemoveField(model_name='simcaplsymbol', name='name_en'),
        migrations.RemoveField(model_name='simcaplsymbol', name='name_zh'),
        migrations.RemoveField(model_name='simcaplsymbol', name='options'),
        migrations.RemoveField(model_name='simcaplsymbol', name='simc_revision'),
        migrations.RemoveField(model_name='simcaplsymbol', name='source'),
        migrations.RemoveField(model_name='simcaplsymbol', name='spec'),
        migrations.RemoveField(model_name='simcaplsymbol', name='spec_key'),
        migrations.RemoveField(model_name='simcaplsymbol', name='spell_id'),
        migrations.RemoveField(model_name='simcaplsymbol', name='trait_id'),
        migrations.RemoveField(model_name='simcaplsymbol', name='wow_build'),
        migrations.AddIndex(
            model_name='simcaplsymbol',
            index=models.Index(fields=['symbol_kind', 'token'], name='simc_sym_kind_token_idx'),
        ),
        migrations.AddConstraint(
            model_name='simcaplsymbol',
            constraint=models.UniqueConstraint(fields=('token', 'symbol_kind'), name='simc_symbol_token_kind_uniq'),
        ),
        migrations.AlterField(
            model_name='simcaplsymbolscope',
            name='symbol',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scopes', to='botend.simcaplsymbol'),
        ),
        migrations.AddIndex(
            model_name='simcaplsymbolscope',
            index=models.Index(fields=['class_name', 'spec', 'hero_tree', 'is_active'], name='simc_scope_visibility_idx'),
        ),
        migrations.AddIndex(
            model_name='simcaplsymbolscope',
            index=models.Index(fields=['spell_id'], name='simc_scope_spell_idx'),
        ),
        migrations.AddIndex(
            model_name='simcaplsymbolscope',
            index=models.Index(fields=['trait_id'], name='simc_scope_trait_idx'),
        ),
        migrations.AddConstraint(
            model_name='simcaplsymbolscope',
            constraint=models.UniqueConstraint(fields=('symbol', 'class_key', 'spec_key', 'hero_tree_key'), name='simc_scope_symbol_scope_uniq'),
        ),
        migrations.AddConstraint(
            model_name='simcaplsymbolscope',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('class_key', ''), ('class_name__isnull', True)), models.Q(('class_name__isnull', False), models.Q(('class_name', ''), _negated=True), ('class_name', models.F('class_key'))), _connector='OR'), name='simc_scope_class_key_ck'),
        ),
        migrations.AddConstraint(
            model_name='simcaplsymbolscope',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('spec__isnull', True), ('spec_key', '')), models.Q(('spec__isnull', False), models.Q(('spec', ''), _negated=True), ('spec', models.F('spec_key'))), _connector='OR'), name='simc_scope_spec_key_ck'),
        ),
        migrations.AddConstraint(
            model_name='simcaplsymbolscope',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('hero_tree__isnull', True), ('hero_tree_key', '')), models.Q(('hero_tree__isnull', False), models.Q(('hero_tree', ''), _negated=True), ('hero_tree', models.F('hero_tree_key'))), _connector='OR'), name='simc_scope_hero_key_ck'),
        ),
    ]
