# 攻略动态标签，以及原有版本、类型标签的迁移。

from django.db import migrations, models


def seed_tags(apps, schema_editor):
    Guide = apps.get_model('botend', 'ClassGuide')
    Tag = apps.get_model('botend', 'ClassGuideTag')
    alias = schema_editor.connection.alias
    labels = {'raid':'团本','mythic-plus':'大秘境','leveling':'练级','pvp':'玩家对战','general':'通用'}
    for guide in Guide.objects.using(alias).iterator():
        for name in [guide.game_version, labels.get(guide.guide_type, guide.guide_type)]:
            if name:
                tag, _ = Tag.objects.using(alias).get_or_create(name=name)
                guide.tags.add(tag)


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0208_class_guide_markdown'),
    ]

    operations = [
        migrations.CreateModel(
            name='ClassGuideTag',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=60, unique=True, verbose_name='标签')),
            ],
            options={
                'verbose_name': '攻略标签',
                'verbose_name_plural': '攻略标签',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='classguide',
            name='tags',
            field=models.ManyToManyField(blank=True, related_name='guides', to='botend.classguidetag', verbose_name='标签'),
        ),
        migrations.RunPython(seed_tags, migrations.RunPython.noop),
    ]
