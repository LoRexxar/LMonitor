from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0079_add_spec_detail_tables'),
    ]

    operations = [
        migrations.AddField(
            model_name='monitortask',
            name='modified_at',
            field=models.DateTimeField(default=None, null=True, verbose_name='后台修改时间'),
        ),
    ]
