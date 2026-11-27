from django import template

from botend.services.published_wow_icons import published_wow_icon_url

register = template.Library()
register.filter('published_wow_icon_url', published_wow_icon_url)
