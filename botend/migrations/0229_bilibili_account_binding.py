# 由 Django 5.2.13 于 2026-09-28 生成：B 站身份绑定、验证申请与配置。

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0228_hotfix_region_facts_and_class_projection'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BilibiliBindingConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=False)),
                ('require_for_registration', models.BooleanField(default=False)),
                ('dynamic_url', models.URLField(blank=True, default='', max_length=500)),
                ('challenge_minutes', models.PositiveSmallIntegerField(default=15)),
                ('check_interval_seconds', models.PositiveSmallIntegerField(default=30)),
                ('max_comment_pages', models.PositiveSmallIntegerField(default=5)),
                ('instructions', models.CharField(blank=True, default='请在指定动态下发表一级评论，请勿回复其他评论。', max_length=500)),
                ('revision', models.UUIDField(default=uuid.uuid4)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='BilibiliAccountBinding',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uid', models.CharField(max_length=20, unique=True)),
                ('nickname', models.CharField(blank=True, default='', max_length=100)),
                ('verified_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('verification_method', models.CharField(default='comment', max_length=20)),
                ('evidence_url', models.URLField(max_length=600)),
                ('comment_id', models.CharField(max_length=30)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('revoke_reason', models.CharField(blank=True, default='', max_length=500)),
                ('revoked_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='bilibili_binding', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='BilibiliBindingChallenge',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('owner_hash', models.CharField(db_index=True, max_length=64)),
                ('requester_hash', models.CharField(db_index=True, max_length=64)),
                ('uid', models.CharField(max_length=20)),
                ('code', models.CharField(max_length=64, unique=True)),
                ('dynamic_url', models.URLField(max_length=500)),
                ('config_revision', models.UUIDField()),
                ('status', models.CharField(default='pending', max_length=20)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('expires_at', models.DateTimeField()),
                ('last_checked_at', models.DateTimeField(blank=True, null=True)),
                ('check_count', models.PositiveIntegerField(default=0)),
                ('last_message', models.CharField(blank=True, default='', max_length=300)),
                ('comment_id', models.CharField(blank=True, default='', max_length=30)),
                ('nickname', models.CharField(blank=True, default='', max_length=100)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('verification_method', models.CharField(blank=True, default='', max_length=20)),
                ('review_note', models.CharField(blank=True, default='', max_length=500)),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='bilibili_challenges', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
    ]
