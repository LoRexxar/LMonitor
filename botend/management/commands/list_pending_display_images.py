"""Offline public game/guide inventory; stdout is publish_site_images input.

Complement tools.discover_editorial_images --urls (published news/report/feed).
Read from a local DB or read replica. No HTTP/OSS, writes, user records, or raw
JSON crawling. Run again when catalog or release changes. Admission is shared
with the publisher; an admissible candidate is NOT evidence of publication.
"""
from django.core.management.base import BaseCommand

from botend.guide_models import ClassGuide
from botend.models import (MythicDungeonSpell, PortalEvent, PortalToolLink,
                           WowSpellSnapshot, WowTalentNodeMetadata, WowItemSnapshot)
from botend.services.class_guide_content import walk_blocks
from botend.services.site_image_assets import published_image_url, source_url
from botend.templatetags.wow_tags import named_icon_source
from tools.discover_editorial_images import slots


def batches(query, *fields, size=512):
    # MySQL client-side cursors can buffer a whole result despite iterator().
    last = 0
    while rows := list(query.filter(pk__gt=last).order_by('pk').values_list('pk', *fields)[:size]):
        for row in rows:
            yield row[1:]
        last = rows[-1][0]


def sources():
    for query in (WowTalentNodeMetadata.all_objects.filter(talent_version__is_active=True),
                  WowSpellSnapshot.objects.all(), WowItemSnapshot.objects.all()):
        for icon, in batches(query.exclude(icon=''), 'icon'):
            yield icon
            for size in ('small', 'large', 'medium'):
                yield named_icon_source(icon, size)
    for name, url in batches(MythicDungeonSpell.objects.filter(is_active=True, data_version__is_active=True),
                             'icon_name', 'icon_url'):
        yield url
        yield named_icon_source(name)
    for model, field in ((PortalEvent, 'image_url'), (PortalToolLink, 'icon_path')):
        for url, in batches(model.objects.filter(is_active=True), field):
            yield url
    for cover, markdown in batches(ClassGuide.objects.filter(archived=False), 'source_payload__featuredImage',
                                  'content_markdown', size=12):
        from botend.services.class_guide_markdown import compile_markdown
        yield cover
        for block in walk_blocks(compile_markdown(markdown)):
            for field in ('title', 'html'):
                yield from slots(block.get(field))


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def handle(self, **options):
        seen = set()
        for value in sources():
            if isinstance(value, str) and value.startswith('//'):
                value = 'https:' + value
            if source_url(value) and value not in seen and not published_image_url(value):
                seen.add(value)
                self.stdout.write(value)
