from django.http import Http404, HttpResponseRedirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from botend.services.character_navigation import character_destination


@never_cache
@require_safe
def character_redirect(request):
    destination = character_destination(request.GET)
    if not destination:
        raise Http404
    return HttpResponseRedirect(destination)
