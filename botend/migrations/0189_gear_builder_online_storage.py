from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('botend', '0188_simc_skill_damage_snapshot_actor'),
    ]

    operations = [
        migrations.CreateModel(
            name='GearBuilderShareLink',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('token', models.CharField(max_length=16, unique=True)),
                ('encoded_state', models.TextField()),
                ('state_hash', models.CharField(db_index=True, max_length=64)),
                ('class_name', models.CharField(max_length=32)),
                ('spec_name', models.CharField(max_length=64)),
                ('batch_key', models.CharField(max_length=160)),
                ('access_count', models.PositiveIntegerField(default=0)),
                ('last_accessed_at', models.DateTimeField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gear_builder_share_links', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'gear_builder_share_link',
                'ordering': ('-created_at', '-id'),
                'indexes': [
                    models.Index(fields=['user', '-created_at'], name='gear_share_user_created_idx'),
                    models.Index(fields=['token', 'is_active'], name='gear_share_token_active_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='GearBuilderUserLoadout',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=80)),
                ('encoded_state', models.TextField()),
                ('state_hash', models.CharField(db_index=True, max_length=64)),
                ('class_name', models.CharField(max_length=32)),
                ('spec_name', models.CharField(max_length=64)),
                ('batch_key', models.CharField(max_length=160)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gear_builder_loadouts', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'gear_builder_user_loadout',
                'ordering': ('-updated_at', '-id'),
                'indexes': [
                    models.Index(fields=['user', '-updated_at'], name='gear_loadout_user_updated_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('user', 'class_name', 'spec_name', 'name'), name='uniq_gear_loadout_user_spec_name'),
                ],
            },
        ),
    ]
