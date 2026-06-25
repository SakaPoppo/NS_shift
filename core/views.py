from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView


class TopPageView(TemplateView):
    template_name = "core/top_page.html"
    authenticated_redirect_url = reverse_lazy("core:main_page")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.authenticated_redirect_url)
        return super().dispatch(request, *args, **kwargs)


class MainPageView(LoginRequiredMixin, TemplateView):
    template_name = "core/main_page.html"


def health_check(_request):
    return HttpResponse("ok", content_type="text/plain")
