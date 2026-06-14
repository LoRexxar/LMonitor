from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0083_wowtalentnodemetadata_display_spell_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='playerspectopplayer',
            name='talent_build_code',
            field=models.TextField(blank=True, default='', help_text='原始天赋导入字符串', verbose_name='天赋字符串'),
        ),
        migrations.AddField(
            model_name='specdungeonranking',
            name='talent_build_code',
            field=models.TextField(blank=True, default='', help_text='原始天赋导入字符串', verbose_name='天赋字符串'),
        ),
        migrations.AddField(
            model_name='specraidranking',
            name='talent_build_code',
            field=models.TextField(blank=True, default='', help_text='原始天赋导入字符串', verbose_name='天赋字符串'),
        ),
    ]
