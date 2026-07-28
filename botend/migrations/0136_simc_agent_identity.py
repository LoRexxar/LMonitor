# Generated for the independent multi-node SimC agent control plane.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('botend', '0135_simcprofile_version')]
    operations = [
        migrations.CreateModel(
            name='SimcAgent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('host_identifier', models.CharField(max_length=128, unique=True)),
                ('name', models.CharField(blank=True, default='', max_length=100)),
                ('is_active', models.BooleanField(default=True)),
                ('status', models.CharField(choices=[('unregistered', 'Unregistered'), ('online', 'Online'), ('busy', 'Busy'), ('degraded', 'Degraded')], default='unregistered', max_length=16)),
                ('platform', models.CharField(default='', max_length=32)),
                ('agent_version', models.CharField(blank=True, default='', max_length=64)),
                ('protocol_version', models.PositiveIntegerField(default=1)),
                ('capabilities', models.JSONField(blank=True, default=dict)),
                ('instance_id', models.CharField(blank=True, default='', max_length=128)),
                ('current_version', models.CharField(blank=True, default='', max_length=128)),
                ('binary_available', models.BooleanField(default=False)),
                ('token_id', models.CharField(blank=True, max_length=32, null=True, unique=True)),
                ('token_hash', models.CharField(blank=True, default='', max_length=255)),
                ('registered_at', models.DateTimeField(blank=True, null=True)),
                ('last_seen_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('backend', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='agents', to='botend.simcbackendbinary')),
            ],
            options={'db_table': 'simc_agent'},
        ),
        migrations.AddConstraint(model_name='simcagent', constraint=models.CheckConstraint(condition=models.Q(status__in=('unregistered', 'online', 'busy', 'degraded')), name='simc_agent_status_ck')),
        migrations.AddConstraint(model_name='simcagent', constraint=models.CheckConstraint(condition=(models.Q(token_hash='') & (models.Q(token_id__isnull=True) | models.Q(token_id=''))) | (~models.Q(token_hash='') & models.Q(token_id__isnull=False) & ~models.Q(token_id='')), name='simc_agent_token_pair_ck')),
    ]
