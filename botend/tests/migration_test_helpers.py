"""Restore the current migration graph after historical-schema tests."""
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def restore_migration_schema():
    """Use repository leaves, not the historical migration under test."""
    # Assertions have finished. Discard this test's rows before replaying data
    # migrations: irreversible canonicalization may otherwise collide with rows
    # that survived the backwards schema transition. Do not emit post_migrate
    # against the temporary historical schema.
    call_command('flush', verbosity=0, interactive=False, inhibit_post_migrate=True)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
