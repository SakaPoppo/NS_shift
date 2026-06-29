from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from .models import StaffMember


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
