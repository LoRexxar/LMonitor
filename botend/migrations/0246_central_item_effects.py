from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0245_ai_provider_configuration')]

    operations = [
        migrations.AddField('wowitemsnapshot', 'effects', models.JSONField(
            default=dict, blank=True, help_text='按分支共用的特效文本、模板和来源；不依赖难度匹配')),
        migrations.AddField('wowitemvariantsnapshot', 'effect_values', models.JSONField(
            default=dict, blank=True, help_text='中央特效引用与此变体参数；effects_json 保留采集原文兼容')),
    ]
