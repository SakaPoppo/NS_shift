from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import StaffMemberForm
from .models import StaffMember, StaffRegularDayOff


def sync_regular_days_off(staff_member, regular_days_off):
    """スタッフの固定休をフォーム入力へ同期する。

    差分更新ではなく、既存レコードを全削除してから再作成する。
    途中で失敗した場合に削除だけが反映されないよう、呼び出し側の
    transaction.atomic() 内で実行する前提にしている。
    """
    staff_member.regular_days_off.all().delete()
    StaffRegularDayOff.objects.bulk_create(
        [
            StaffRegularDayOff(staff_member=staff_member, day_of_week=day_of_week)
            for day_of_week in regular_days_off
        ]
    )


class UserStaffMemberQuerysetMixin(LoginRequiredMixin):
    """ログイン中ユーザーが管理する在籍スタッフだけを扱うMixin。"""

    model = StaffMember
    success_url = reverse_lazy("staff:list")

    def get_queryset(self):
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off")


class StaffMemberListView(UserStaffMemberQuerysetMixin, ListView):
    template_name = "staff/staff_member_list.html"
    context_object_name = "staff_members"

    def get_queryset(self):
        return super().get_queryset().order_by("id")


class StaffMemberCreateView(LoginRequiredMixin, CreateView):
    model = StaffMember
    form_class = StaffMemberForm
    template_name = "staff/staff_member_create.html"
    success_url = reverse_lazy("staff:list")

    def form_valid(self, form):
        # StaffMember と固定休は別テーブルなので、片方だけ保存される状態を避ける。
        with transaction.atomic():
            form.instance.user = self.request.user
            self.object = form.save()
            sync_regular_days_off(
                self.object,
                form.cleaned_data.get("regular_days_off", []),
            )

        return HttpResponseRedirect(self.get_success_url())


class StaffMemberUpdateView(UserStaffMemberQuerysetMixin, UpdateView):
    form_class = StaffMemberForm
    template_name = "staff/staff_member_edit.html"

    def form_valid(self, form):
        # 更新時も StaffMember 本体と固定休を同じトランザクションで扱う。
        with transaction.atomic():
            self.object = form.save()
            sync_regular_days_off(
                self.object,
                form.cleaned_data.get("regular_days_off", []),
            )

        return HttpResponseRedirect(self.get_success_url())


class StaffMemberDeleteView(UserStaffMemberQuerysetMixin, DeleteView):
    template_name = "staff/staff_member_confirm_delete.html"

    def form_valid(self, form):
        self.object = self.get_object()
        # 物理削除すると関連データまで失われるため、一覧や今後の対象から外すだけに留める。
        self.object.is_active = False
        self.object.save()
        return HttpResponseRedirect(self.get_success_url())
