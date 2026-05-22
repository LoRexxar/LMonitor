from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0069_monitortask_proxy_fields'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='monitortask',
            name='proxy_bypass',
        ),
        migrations.RemoveField(
            model_name='monitortask',
            name='proxy_scope',
        ),
        migrations.RemoveField(
            model_name='monitortask',
            name='proxy_url',
        ),
    ]

