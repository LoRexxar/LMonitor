from django.views import View
from django.shortcuts import render


class PortalHomeView(View):
    def get(self, request):
        return render(request, 'portal/index.html')

