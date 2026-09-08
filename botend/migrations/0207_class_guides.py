# 职业攻略、版本修订、来源同步与术语校订。

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0206_move_simc_baselines_navigation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ClassGuide',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255, verbose_name='标题')),
                ('slug', models.SlugField(max_length=200, verbose_name='标识')),
                ('class_name', models.CharField(max_length=32, verbose_name='职业')),
                ('spec_name', models.CharField(max_length=32, verbose_name='专精')),
                ('game_version', models.CharField(max_length=64, verbose_name='游戏版本')),
                ('guide_type', models.CharField(default='raid', max_length=40, verbose_name='攻略类型')),
                ('source_url', models.URLField(blank=True, max_length=1000, verbose_name='来源')),
                ('author', models.CharField(blank=True, max_length=200, verbose_name='原作者')),
                ('archived', models.BooleanField(default=False, verbose_name='已归档')),
                ('revision_number', models.PositiveIntegerField(default=0, verbose_name='修订序号')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': '职业攻略',
                'verbose_name_plural': '职业攻略',
            },
        ),
        migrations.CreateModel(
            name='ClassGuideFeed',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(default='maxroll', max_length=64, unique=True)),
                ('enabled', models.BooleanField(default=False, verbose_name='启用监控')),
                ('interval_minutes', models.PositiveIntegerField(default=360, verbose_name='检查间隔（分钟）')),
                ('authorization_note', models.TextField(blank=True, verbose_name='授权说明')),
                ('last_checked_at', models.DateTimeField(blank=True, null=True)),
                ('next_check_at', models.DateTimeField(blank=True, null=True)),
                ('lease_until', models.DateTimeField(blank=True, null=True)),
                ('lease_token', models.CharField(blank=True, max_length=64)),
            ],
            options={
                'verbose_name': '攻略同步设置',
                'verbose_name_plural': '攻略同步设置',
            },
        ),
        migrations.CreateModel(
            name='ClassGuideSyncRun',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(default='running', max_length=24, verbose_name='状态')),
                ('discovered', models.JSONField(default=list, verbose_name='目录清单')),
                ('results', models.JSONField(default=list, verbose_name='逐篇结果')),
                ('coverage', models.JSONField(default=dict, verbose_name='覆盖率')),
                ('error', models.TextField(blank=True, verbose_name='错误')),
                ('started_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'verbose_name': '攻略同步批次',
                'verbose_name_plural': '攻略同步批次',
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='ClassGuideTranslation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=64, unique=True)),
                ('source', models.TextField(verbose_name='原文')),
                ('translated', models.TextField(verbose_name='译文')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': '攻略翻译缓存',
                'verbose_name_plural': '攻略翻译缓存',
            },
        ),
        migrations.CreateModel(
            name='ClassGuideRevision',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.PositiveIntegerField(verbose_name='序号')),
                ('origin', models.CharField(default='manual', max_length=20, verbose_name='来源类型')),
                ('title', models.CharField(max_length=255, verbose_name='修订标题')),
                ('blocks', models.JSONField(default=list, verbose_name='中文内容块')),
                ('source_blocks', models.JSONField(default=list, verbose_name='原文内容块')),
                ('source_payload', models.JSONField(default=dict, verbose_name='原始来源快照')),
                ('source_hash', models.CharField(blank=True, db_index=True, max_length=64, verbose_name='来源指纹')),
                ('source_modified', models.CharField(blank=True, max_length=80, verbose_name='来源更新时间')),
                ('audit', models.JSONField(default=dict, verbose_name='完整性审核')),
                ('note', models.CharField(blank=True, max_length=500, verbose_name='修订说明')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('guide', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='revisions', to='botend.classguide')),
            ],
            options={
                'verbose_name': '攻略修订',
                'verbose_name_plural': '攻略修订',
                'ordering': ['-number'],
            },
        ),
        migrations.AddField(
            model_name='classguide',
            name='published_revision',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='botend.classguiderevision', verbose_name='已审核版本'),
        ),
        migrations.CreateModel(
            name='ClassGuideTerm',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('game_version', models.CharField(max_length=64, verbose_name='版本')),
                ('kind', models.CharField(max_length=16, verbose_name='引用类型')),
                ('object_id', models.PositiveBigIntegerField(verbose_name='对象编号')),
                ('name_en', models.CharField(blank=True, max_length=255, verbose_name='英文')),
                ('name_zh', models.CharField(max_length=255, verbose_name='官方中文')),
                ('icon', models.CharField(blank=True, max_length=255, verbose_name='图标')),
                ('evidence', models.CharField(max_length=1000, verbose_name='核对依据')),
            ],
            options={
                'verbose_name': '攻略术语校订',
                'verbose_name_plural': '攻略术语校订',
                'constraints': [models.UniqueConstraint(fields=('game_version', 'kind', 'object_id'), name='guide_term_identity_unique')],
            },
        ),
        migrations.AddConstraint(
            model_name='classguiderevision',
            constraint=models.UniqueConstraint(fields=('guide', 'number'), name='guide_revision_number_unique'),
        ),
        migrations.AddIndex(
            model_name='classguide',
            index=models.Index(fields=['class_name', 'spec_name', 'game_version', 'guide_type'], name='guide_catalog_idx'),
        ),
        migrations.AddConstraint(
            model_name='classguide',
            constraint=models.UniqueConstraint(fields=('slug', 'game_version'), name='guide_slug_version_unique'),
        ),
    ]
