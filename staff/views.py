from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction #トランザクション処理
from django.http import HttpResponseRedirect 
from django.urls import reverse_lazy #逆引き用
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import StaffMemberForm
from .models import StaffMember, StaffRegularDayOff


def sync_regular_days_off(staff_member, regular_days_off): #固定休を更新する用の関数
    staff_member.regular_days_off.all().delete() #既存の固定休を削除
    StaffRegularDayOff.objects.bulk_create( #リストを作って一気にDBに保存するためのもの
        [
            StaffRegularDayOff(staff_member=staff_member, day_of_week=day_of_week)
            for day_of_week in regular_days_off
        ]
    )


class UserStaffMemberQuerysetMixin(LoginRequiredMixin): #共通の処理まとめ,スタッフの呼び出し
    model = StaffMember #使用するモデル
    success_url = reverse_lazy("staff:list") #リダイレクト先

    def get_queryset(self): #スタッフの取得条件　1,ログインユーザーのスタッフ　2,在籍中のスタッフ　
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off") #上の奴らの固定休を取得


class StaffMemberListView(UserStaffMemberQuerysetMixin, ListView):
    template_name = "staff/staff_member_list.html"
    context_object_name = "staff_members" #HTMLで使う変数名を指定するためのもの

    def get_queryset(self):
        return super().get_queryset().order_by("id")


class StaffMemberCreateView(LoginRequiredMixin, CreateView):
    model = StaffMember
    form_class = StaffMemberForm
    template_name = "staff/staff_member_create.html"
    success_url = reverse_lazy("staff:list")

    def form_valid(self, form): #
        with transaction.atomic(): #DB１＝OK、DB２＝NGの時とかに、処理自体をもなかったことにする
            form.instance.user = self.request.user #ログインユーザーをスタッフに紐付け
            self.object = form.save() #スタッフを保存
            sync_regular_days_off(
                self.object,
                form.cleaned_data.get("regular_days_off", []),
            )

        return HttpResponseRedirect(self.get_success_url())


class StaffMemberUpdateView(UserStaffMemberQuerysetMixin, UpdateView):
    form_class = StaffMemberForm
    template_name = "staff/staff_member_edit.html"

    def form_valid(self, form):
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
        self.object.is_active = False #論理削除
        self.object.save()
        return HttpResponseRedirect(self.get_success_url())
