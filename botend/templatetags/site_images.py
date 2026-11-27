"""Read-only template adapters; publish decisions stay in the asset service."""
from django import template
from botend.services import site_image_assets
from botend.site_image_map import release_script

register = template.Library()


@register.filter
def published_image_url(url):
    return site_image_assets.published_image_url(url)


@register.filter
def journal_art_url(file_id):
    if not file_id:
        return ''
    return site_image_assets.published_image_url(f'/portal/adventure-journal/art/{file_id}/?v=2')


@register.simple_tag
def published_image_version():
    return release_script()[0]
