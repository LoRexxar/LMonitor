from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0153_dashboardusergroup_dashboardusergroupmembership_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='dashboardusergroup',
            name='permission_codes',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name='dashboardusergroupmembership',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='dashboard_user_group_memberships',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name='dashboardusergroupmembership',
            constraint=models.UniqueConstraint(
                fields=('user', 'group'),
                name='unique_dashboard_user_group_membership',
            ),
        ),
    ]
