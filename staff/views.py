from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, FormView, ListView, TemplateView, UpdateView

from .constants import MAX_ACTIVE_STAFF_COUNT, active_staff_limit_message
from .forms import BulkStaffMemberFormSet, BulkStaffSetupForm, StaffMemberForm
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


class ActiveStaffLimitMixin:
    """アクティブスタッフの上限を新規作成時にだけ適用する。"""

    def get_active_staff_count(self):
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).count()

    def is_active_staff_limit_reached(self):
        return self.get_active_staff_count() >= MAX_ACTIVE_STAFF_COUNT

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and self.is_active_staff_limit_reached():
            messages.error(request, active_staff_limit_message())
            return redirect("staff:list")

        return super().dispatch(request, *args, **kwargs)


class StaffMemberListView(UserStaffMemberQuerysetMixin, ListView):
    template_name = "staff/staff_member_list.html"
    context_object_name = "staff_members"

    def get_queryset(self):
        return super().get_queryset().order_by("-ability_level", "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_staff_count = self.get_queryset().count()
        context.update(
            active_staff_count=active_staff_count,
            max_active_staff_count=MAX_ACTIVE_STAFF_COUNT,
            can_create_staff=active_staff_count < MAX_ACTIVE_STAFF_COUNT,
        )
        return context


class StaffMemberCreateView(ActiveStaffLimitMixin, LoginRequiredMixin, CreateView):
    model = StaffMember
    form_class = StaffMemberForm
    template_name = "staff/staff_member_create.html"
    success_url = reverse_lazy("staff:list")

    def form_valid(self, form):
        # StaffMember と固定休は別テーブルなので、片方だけ保存される状態を避ける。
        with transaction.atomic():
            # 同時に複数の登録リクエストが来ても、ユーザー単位で上限判定を直列化する。
            get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
            if self.is_active_staff_limit_reached():
                messages.error(self.request, active_staff_limit_message())
                return redirect("staff:list")

            form.instance.user = self.request.user
            self.object = form.save()
            sync_regular_days_off(
                self.object,
                form.cleaned_data.get("regular_days_off", []),
            )

        return HttpResponseRedirect(self.get_success_url())


class BulkStaffSetupView(LoginRequiredMixin, FormView):
    """人数設定を受け付け、確認用フォームの初期値だけをセッションに保持する。"""

    form_class = BulkStaffSetupForm
    template_name = "staff/bulk_staff_setup.html"
    success_url = reverse_lazy("staff:bulk_create_confirm")
    session_key = "bulk_staff_member_initial_data"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        active_staff_count = StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).count()
        context.update(
            active_staff_count=active_staff_count,
            remaining_staff_count=max(MAX_ACTIVE_STAFF_COUNT - active_staff_count, 0),
            max_active_staff_count=MAX_ACTIVE_STAFF_COUNT,
        )
        return context

    def form_valid(self, form):
        # この段階では StaffMember を保存せず、確認用フォームの初期値だけを保持する。
        # フォーム1の再送信は新しい一括登録の開始として、以前の確認データを置き換える。
        self.request.session.pop(self.session_key, None)
        self.request.session[self.session_key] = form.build_member_initial_data()
        return super().form_valid(form)


class BulkStaffCreateConfirmView(LoginRequiredMixin, TemplateView):
    """一括登録の確認・保存を行う。"""

    template_name = "staff/bulk_staff_confirm.html"
    session_key = BulkStaffSetupView.session_key

    def get_initial_data(self):
        return self.request.session.get(self.session_key, [])

    def dispatch(self, request, *args, **kwargs):
        if not self.get_initial_data():
            messages.error(request, "先に一括登録するスタッフ数を入力してください。")
            return redirect("staff:bulk_create")
        return super().dispatch(request, *args, **kwargs)

    def get_formset(self, data=None):
        initial_data = self.get_initial_data()
        kwargs = {
            "queryset": StaffMember.objects.none(),
            "expected_form_count": len(initial_data),
            "initial": initial_data,
        }
        if data is not None:
            kwargs["data"] = data
        return BulkStaffMemberFormSet(**kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("formset", self.get_formset())
        context.setdefault("registration_count", len(self.get_initial_data()))
        return context

    def get(self, request, *args, **kwargs):
        return self.render_to_response(self.get_context_data())

    def post(self, request, *args, **kwargs):
        formset = self.get_formset(data=request.POST)

        if not formset.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "formset": formset,
                    "registration_count": len(self.get_initial_data()),
                },
            )

        if not self.save_formset(formset):
            messages.error(request, active_staff_limit_message())
            return render(
                request,
                self.template_name,
                {
                    "formset": formset,
                    "registration_count": len(self.get_initial_data()),
                },
            )

        self.request.session.pop(self.session_key, None)
        messages.success(request, f"{formset.total_form_count()}人のスタッフを登録しました。")
        return redirect("staff:list")

    def save_formset(self, formset):
        """上限確認とスタッフ・固定休の保存を同一トランザクションで行う。"""
        with transaction.atomic():
            # 単体登録と同じく、ユーザー行をロックして上限判定を直列化する。
            user = get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
            active_staff_count = StaffMember.objects.filter(
                user=user,
                is_active=True,
            ).count()
            if active_staff_count + formset.total_form_count() > MAX_ACTIVE_STAFF_COUNT:
                return False

            for form in formset.forms:
                staff_member = form.save(commit=False)
                staff_member.user = user
                staff_member.save()
                sync_regular_days_off(
                    staff_member,
                    form.cleaned_data.get("regular_days_off", []),
                )

        return True


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
