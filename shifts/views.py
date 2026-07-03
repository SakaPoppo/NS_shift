from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from .models import ShiftPlan


class ShiftPlanListView(LoginRequiredMixin, ListView):
    model = ShiftPlan
    template_name = "shifts/shift_plan_list.html"
    context_object_name = "shift_plans"

    def get_queryset(self):
        return ShiftPlan.objects.filter(user=self.request.user)
