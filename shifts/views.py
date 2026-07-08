from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView

from .forms import ShiftPlanCreateForm
from .models import ShiftPlan


class ShiftPlanListView(LoginRequiredMixin, ListView):
    model = ShiftPlan
    template_name = "shifts/shift_plan_list.html"
    context_object_name = "shift_plans"

    def get_queryset(self):
        return ShiftPlan.objects.filter(user=self.request.user)


class ShiftPlanCreateView(LoginRequiredMixin, CreateView):
    model = ShiftPlan
    form_class = ShiftPlanCreateForm
    template_name = "shifts/shift_plan_create.html"
    success_url = reverse_lazy("shifts:list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.user = self.request.user
        self.object = form.save()
        # TODO: 次のシフト作成画面が実装されたら、ここを作成フローの次画面へ変更する
        return HttpResponseRedirect(self.get_success_url())
