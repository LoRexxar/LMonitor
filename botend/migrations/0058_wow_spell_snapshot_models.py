from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("botend", "0057_wowskilldiffreport_display_builds"),
    ]

    operations = [
        migrations.CreateModel(
            name="WowSpellSnapshotState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("branch", models.CharField(default="wow", max_length=32)),
                ("locale", models.CharField(default="enUS", max_length=8)),
                ("snapshot_build", models.CharField(default="", max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "wow_spell_snapshot_state",
                "unique_together": {("branch", "locale")},
            },
        ),
        migrations.CreateModel(
            name="WowSpellSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("branch", models.CharField(default="wow", max_length=32)),
                ("locale", models.CharField(default="enUS", max_length=8)),
                ("spell_id", models.BigIntegerField()),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("aura_description", models.TextField(blank=True, default="")),
                ("snapshot_build", models.CharField(blank=True, default="", max_length=64)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "wow_spell_snapshot",
                "unique_together": {("branch", "locale", "spell_id")},
            },
        ),
        migrations.CreateModel(
            name="WowSpellEffectSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("branch", models.CharField(default="wow", max_length=32)),
                ("locale", models.CharField(default="enUS", max_length=8)),
                ("spell_id", models.BigIntegerField()),
                ("effect_index", models.IntegerField(default=0)),
                ("effect", models.IntegerField(blank=True, null=True)),
                ("effect_aura", models.IntegerField(blank=True, null=True)),
                ("base_points", models.CharField(blank=True, default="", max_length=64)),
                ("coefficient", models.CharField(blank=True, default="", max_length=64)),
                ("pvp_multiplier", models.CharField(blank=True, default="", max_length=64)),
                ("snapshot_build", models.CharField(blank=True, default="", max_length=64)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "wow_spell_effect_snapshot",
                "unique_together": {("branch", "locale", "spell_id", "effect_index")},
            },
        ),
        migrations.CreateModel(
            name="WowSpecSpellMapSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("branch", models.CharField(default="wow", max_length=32)),
                ("locale", models.CharField(default="enUS", max_length=8)),
                ("spec_id", models.IntegerField()),
                ("spell_id", models.BigIntegerField()),
                ("snapshot_build", models.CharField(blank=True, default="", max_length=64)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                "db_table": "wow_spec_spell_map_snapshot",
                "unique_together": {("branch", "locale", "spec_id", "spell_id")},
            },
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshotstate",
            index=models.Index(fields=["branch", "locale"], name="wow_spell_s_branch__55b8a0_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshotstate",
            index=models.Index(fields=["snapshot_build"], name="wow_spell_s_snapshot_4b1cde_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshotstate",
            index=models.Index(fields=["updated_at"], name="wow_spell_s_updated__1b8e6a_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshot",
            index=models.Index(fields=["branch", "locale"], name="wow_spell_branch__ed17a0_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshot",
            index=models.Index(fields=["spell_id"], name="wow_spell_spell_id_7d8a4d_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspellsnapshot",
            index=models.Index(fields=["updated_at"], name="wow_spell_updated__530fb4_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspelleffectsnapshot",
            index=models.Index(fields=["branch", "locale"], name="wow_spell_e_branch__dc0b4f_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspelleffectsnapshot",
            index=models.Index(fields=["spell_id"], name="wow_spell_e_spell_id_55c2cb_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspelleffectsnapshot",
            index=models.Index(fields=["spell_id", "effect_index"], name="wow_spell_e_spell_i_6b7e74_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspelleffectsnapshot",
            index=models.Index(fields=["updated_at"], name="wow_spell_e_updated__f9530c_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspecspellmapsnapshot",
            index=models.Index(fields=["branch", "locale"], name="wow_spec_s_branch__fc8c5f_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspecspellmapsnapshot",
            index=models.Index(fields=["spec_id"], name="wow_spec_s_spec_id_0d1b6d_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspecspellmapsnapshot",
            index=models.Index(fields=["spell_id"], name="wow_spec_s_spell_id_92f7f7_idx"),
        ),
        migrations.AddIndex(
            model_name="wowspecspellmapsnapshot",
            index=models.Index(fields=["updated_at"], name="wow_spec_s_updated__63b0c4_idx"),
        ),
    ]

