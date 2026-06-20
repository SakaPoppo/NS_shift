from django.views.generic import TemplateView


class TopPageView(TemplateView):
    template_name = "core/top_page.html"
