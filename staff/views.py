from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, ListView

from .forms import StaffMemberCreateForm
from .models import StaffMember
from .models import StaffRegularDayOff


class StaffMemberListView(LoginRequiredMixin, ListView):
    model = StaffMember
    template_name = "staff/staff_member_list.html"
    context_object_name = "staff_members"

    def get_queryset(self):
        return (
            StaffMember.objects.filter(user=self.request.user, is_active=True)
            .prefetch_related("regular_days_off")
            .order_by("id")
        )


class StaffMemberCreateView(LoginRequiredMixin, CreateView):
    model = StaffMember
    form_class = StaffMemberCreateForm
    template_name = "staff/staff_member_create.html"
    success_url = reverse_lazy("staff:list")

    def form_valid(self, form):
        with transaction.atomic():
            form.instance.user = self.request.user
            self.object = form.save()

            regular_days_off = form.cleaned_data.get("regular_days_off", [])
            StaffRegularDayOff.objects.bulk_create(
                [
                    StaffRegularDayOff(staff_member=self.object, day_of_week=day_of_week)
                    for day_of_week in regular_days_off
                ]
            )

        return HttpResponseRedirect(self.get_success_url())
