# Generated manually for talent spec component metadata

from django.db import migrations, models


def add_columns_if_missing(apps, schema_editor):
    table = 'wow_talent_node_metadata'
    connection = schema_editor.connection
    existing_columns = {col.name for col in connection.introspection.get_table_description(connection.cursor(), table)}
    with connection.cursor() as cursor:
        if 'db2_subtree_id' not in existing_columns:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN db2_subtree_id INT NOT NULL DEFAULT 0')
        if 'db2_tree_id' not in existing_columns:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN db2_tree_id INT NULL')
        if 'db2_component_id' not in existing_columns:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN db2_component_id INT NOT NULL DEFAULT 0')


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0086_wowitemsnapshot'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(add_columns_if_missing, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='wowtalentnodemetadata',
                    name='db2_subtree_id',
                    field=models.IntegerField(blank=True, default=0),
                ),
                migrations.AddField(
                    model_name='wowtalentnodemetadata',
                    name='db2_tree_id',
                    field=models.IntegerField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='wowtalentnodemetadata',
                    name='db2_component_id',
                    field=models.IntegerField(blank=True, default=0),
                ),
            ],
        ),
    ]
