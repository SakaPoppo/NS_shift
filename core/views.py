from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from shifts.models import ShiftPlan
from staff.models import StaffMember


class TopPageView(TemplateView):
    template_name = "core/top_page.html"
    authenticated_redirect_url = reverse_lazy("core:main_page")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(self.authenticated_redirect_url)
        return super().dispatch(request, *args, **kwargs)


class MainPageView(LoginRequiredMixin, TemplateView):
    template_name = "core/main_page.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        staff_count = StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).count()

        if staff_count == 0:
            dashboard_state = "setup_staff"
        elif not ShiftPlan.objects.filter(user=self.request.user).exists():
            dashboard_state = "create_shift"
        else:
            dashboard_state = "normal"

        context.update(
            dashboard_state=dashboard_state,
            staff_count=staff_count,
        )
        return context


def health_check(_request):
    return HttpResponse("ok", content_type="text/plain")
