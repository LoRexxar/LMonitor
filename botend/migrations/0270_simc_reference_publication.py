# 前台专精资料改为显式发布清单，默认不公开任何资源。

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0269_simc_reference_simulations'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimcReferencePublication',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('spec_key', models.CharField(max_length=100, unique=True)),
                ('is_published', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('apls', models.ManyToManyField(blank=True, to='botend.simcapl')),
                ('profiles', models.ManyToManyField(blank=True, to='botend.simcprofile')),
                ('talents', models.ManyToManyField(blank=True, to='botend.simctalentstring')),
            ],
            options={
                'verbose_name': '前台专精公开范围',
                'verbose_name_plural': '前台专精公开范围',
            },
        ),
    ]
