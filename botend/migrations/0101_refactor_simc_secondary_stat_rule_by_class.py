"""
重构 SimcSecondaryStatRule：spec → class_name（按职业），新增 SimcMasteryCoefficient（按专精）。
"""
from django.db import migrations, models, connection


SPEC_TO_CLASS = {
    'arms': 'warrior', 'fury': 'warrior', 'protection': 'warrior', 'protection_warrior': 'warrior',
    'havoc': 'demon_hunter', 'vengeance': 'demon_hunter',
    'balance': 'druid', 'feral': 'druid', 'guardian': 'druid', 'restoration_druid': 'druid',
    'devastation': 'evoker', 'preservation': 'evoker', 'augmentation': 'evoker',
    'beast_mastery': 'hunter', 'marksmanship': 'hunter', 'survival': 'hunter',
    'arcane': 'mage', 'fire': 'mage', 'frost_mage': 'mage',
    'brewmaster': 'monk', 'mistweaver': 'monk', 'windwalker': 'monk',
    'holy_paladin': 'paladin', 'protection_paladin': 'paladin', 'retribution': 'paladin',
    'discipline': 'priest', 'holy_priest': 'priest', 'shadow': 'priest',
    'assassination': 'rogue', 'outlaw': 'rogue', 'subtlety': 'rogue',
    'elemental': 'shaman', 'enhancement': 'shaman', 'restoration_shaman': 'shaman',
    'affliction': 'warlock', 'demonology': 'warlock', 'destruction': 'warlock',
    'blood': 'death_knight', 'frost_dk': 'death_knight', 'unholy': 'death_knight',
}


def drop_spec_unique_forward(apps, schema_editor):
    """去掉 spec 的 unique 约束"""
    engine = connection.vendor
    if engine == 'mysql':
        with connection.cursor() as cursor:
            # 查找 spec 列上的唯一索引名
            cursor.execute(
                "SHOW INDEX FROM simc_secondary_stat_rule WHERE Column_name='spec' AND Non_unique=0"
            )
            rows = cursor.fetchall()
            for row in rows:
                idx_name = row[2]  # Key_name
                cursor.execute(f"ALTER TABLE simc_secondary_stat_rule DROP INDEX `{idx_name}`")
    # SQLite: Django AlterField 会自动重建表，无需手动处理


def drop_spec_unique_backward(apps, schema_editor):
    """恢复 spec 的 unique 约束"""
    engine = connection.vendor
    if engine == 'mysql':
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE simc_secondary_stat_rule ADD UNIQUE INDEX `spec_uniq` (`spec`)"
            )


def data_migration_forward(apps, schema_editor):
    SimcSecondaryStatRule = apps.get_model('botend', 'SimcSecondaryStatRule')
    SimcMasteryCoefficient = apps.get_model('botend', 'SimcMasteryCoefficient')

    all_rows = list(SimcSecondaryStatRule.objects.all())

    # 1. 提取 mastery_coefficient → 新表
    for row in all_rows:
        SimcMasteryCoefficient.objects.create(
            spec=row.spec,
            mastery_coefficient=row.mastery_coefficient,
        )

    # 2. 按职业去重
    class_data = {}
    for row in all_rows:
        cn = SPEC_TO_CLASS.get(row.spec, row.spec)
        if cn not in class_data:
            class_data[cn] = {
                'crit_per_percent': row.crit_per_percent,
                'haste_per_percent': row.haste_per_percent,
                'mastery_per_percent': row.mastery_per_percent,
                'versatility_per_percent': row.versatility_per_percent,
            }

    # 3. 清空旧表重建（raw SQL 避免 ORM 约束冲突）
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM simc_secondary_stat_rule")
        for cn, vals in class_data.items():
            cursor.execute(
                "INSERT INTO simc_secondary_stat_rule "
                "(spec, class_name, crit_per_percent, haste_per_percent, mastery_per_percent, mastery_coefficient, versatility_per_percent) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [cn, cn, vals['crit_per_percent'], vals['haste_per_percent'],
                 vals['mastery_per_percent'], 1.0, vals['versatility_per_percent']]
            )


def data_migration_backward(apps, schema_editor):
    SimcSecondaryStatRule = apps.get_model('botend', 'SimcSecondaryStatRule')
    SimcMasteryCoefficient = apps.get_model('botend', 'SimcMasteryCoefficient')

    class_rows = list(SimcSecondaryStatRule.objects.all())
    mastery_map = {mc.spec: mc.mastery_coefficient for mc in SimcMasteryCoefficient.objects.all()}

    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM simc_secondary_stat_rule")
        for row in class_rows:
            for spec, cn in SPEC_TO_CLASS.items():
                if cn == row.class_name:
                    cursor.execute(
                        "INSERT INTO simc_secondary_stat_rule "
                        "(spec, crit_per_percent, haste_per_percent, mastery_per_percent, "
                        "mastery_coefficient, versatility_per_percent) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        [spec, row.crit_per_percent, row.haste_per_percent,
                         row.mastery_per_percent, mastery_map.get(spec, 1.4),
                         row.versatility_per_percent]
                    )
    SimcMasteryCoefficient.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0100_remove_simc_profile_fight_fields'),
    ]

    operations = [
        # 1. 去掉 spec 的 unique 约束
        migrations.RunPython(drop_spec_unique_forward, drop_spec_unique_backward),

        # 2. 新增 class_name 列
        migrations.AddField(
            model_name='simcsecondarystatrule',
            name='class_name',
            field=models.CharField(default='', max_length=50, help_text='职业标识'),
        ),

        # 3. 创建 SimcMasteryCoefficient 表
        migrations.CreateModel(
            name='SimcMasteryCoefficient',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('spec', models.CharField(help_text='专精标识，如 fury/arms/fire', max_length=50, unique=True)),
                ('mastery_coefficient', models.FloatField(default=1.4, help_text='精通系数（最终结果乘以该值）')),
            ],
            options={
                'db_table': 'simc_mastery_coefficient',
                'verbose_name': 'SimC精通系数',
                'verbose_name_plural': 'SimC精通系数',
            },
        ),

        # 4. 数据迁移：提取 mastery_coefficient，按职业去重
        migrations.RunPython(data_migration_forward, data_migration_backward),

        # 5. 设置 class_name 为 unique
        migrations.AlterField(
            model_name='simcsecondarystatrule',
            name='class_name',
            field=models.CharField(max_length=50, unique=True, help_text='职业标识，如 warrior/mage/priest'),
        ),

        # 6. 删除旧列
        migrations.RemoveField(model_name='simcsecondarystatrule', name='spec'),
        migrations.RemoveField(model_name='simcsecondarystatrule', name='mastery_coefficient'),
    ]
