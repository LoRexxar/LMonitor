from django.db import migrations


def ensure_server_slots(apps, schema_editor):
    Version = apps.get_model('botend', 'WowTalentVersion')
    versions = Version.objects.using(schema_editor.connection.alias)
    Node = apps.get_model('botend', 'WowTalentNodeMetadata')
    nodes = Node.objects.using(schema_editor.connection.alias)
    for branch, label in (('retail', '正式服'), ('ptr', 'PTR'), ('beta', 'Beta')):
        current = versions.filter(key=branch).first()
        if current and current.branch != branch:
            raise ValueError(f'Talent slot {branch} has conflicting branch {current.branch}')
        populated = list(versions.filter(branch=branch, is_active=True, status='active')
                         .exclude(key=branch).filter(pk__in=nodes.values('talent_version_id')))
        if populated:
            if len(populated) != 1 or (current and nodes.filter(talent_version_id=current.pk).exists()):
                raise ValueError(f'Conflicting populated talent trees for {branch}; explicit reconciliation required')
            if current:
                # Keep even the empty slot's PK/FKs: retire its key rather than delete it.
                current.key = f'retired-slot-{branch}-{current.pk}'
                current.save(update_fields=['key'])
            current = populated[0]
            current.key = branch
            current.save(update_fields=['key'])
        if current is None:
            current = versions.filter(branch=branch, is_active=True, status='active').order_by(
                '-activated_at', '-id').first()
            if current is not None:
                # Preserve PK and all references; build is mutable provenance.
                current.key = branch
                current.save(update_fields=['key'])
            else:
                current = versions.create(key=branch, branch=branch, label=label,
                    status='draft', is_active=False,
                    is_default_simulator=branch == 'retail',
                    is_default_player_tree=branch == 'retail', is_default_stats=branch == 'retail')
        if current.branch != branch:
            raise ValueError(f'Talent slot {branch} has conflicting branch {current.branch}')
        # Historical rows and frozen references remain intact, but are not
        # independently selectable versions. Never cascade-delete old tasks.
        versions.filter(branch=branch).exclude(pk=current.pk).update(
            is_active=False, is_default_simulator=False,
            is_default_player_tree=False, is_default_stats=False)


class Migration(migrations.Migration):
    dependencies = [('botend', '0260_monitor_task_lease_is_manual')]
    operations = [migrations.RunPython(ensure_server_slots, migrations.RunPython.noop)]
