"""Read-only template adapters; publish decisions stay in the asset service."""
from django import template
from botend.services import site_image_assets
from botend.site_image_map import release_script

register = template.Library()


@register.filter
def published_image_url(url):
    return site_image_assets.published_image_url(url)


@register.simple_tag
def published_image_version():
    return release_script()[0]
