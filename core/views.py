from django.http import HttpResponse
from django.views.generic import TemplateView


class TopPageView(TemplateView):
    template_name = "core/top_page.html"


def health_check(_request):
    return HttpResponse("ok", content_type="text/plain")
