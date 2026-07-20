from django.core.management.base import BaseCommand, CommandError

from botend.services.simc_apl.symbol_sync import sync_symbols


class Command(BaseCommand):
    help = '同步同一 SimC revision/WoW build 的可审计 APL symbol facts'

    def add_arguments(self, parser):
        parser.add_argument('--simc-revision', required=True)
        parser.add_argument('--wow-build', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        try:
            summary = sync_symbols(options['simc_revision'], options['wow_build'],
                                   dry_run=options['dry_run'])
        except (TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        prefix = '[DRY-RUN] ' if options['dry_run'] else ''
        message = (
            f"{prefix}created={summary.created} updated={summary.updated} "
            f"unchanged={summary.unchanged} deactivated={summary.deactivated} "
            f"unbound={summary.unbound} invalid={summary.invalid} "
            f"completeness={summary.completeness}"
        )
        self.stdout.write(self.style.SUCCESS(message))
