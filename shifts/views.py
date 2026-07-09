import calendar
from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView

from staff.models import StaffMember

from .forms import ShiftPlanCreateForm, ShiftRuleForm
from .models import DayOffRequest, ShiftPlan, ShiftResult

SHIFT_SELECT_OPTIONS = [  # シフトセルの選択肢を呼び出す
    ("", ""),
    (ShiftResult.ShiftTypeChoices.DAY, "日"),
    (ShiftResult.ShiftTypeChoices.NIGHT, "夜"),
    (ShiftResult.ShiftTypeChoices.AFTER_NIGHT, "明"),
    (ShiftResult.ShiftTypeChoices.OFF, "休"),
    (ShiftResult.ShiftTypeChoices.OFF_REQUEST, "希"),
]

SHIFT_DISPLAY_CONFIG = {  # シフトセルに表示するラベルと色を設定
    ShiftResult.ShiftTypeChoices.DAY: {
        "label": "日",
        "classes": "border-amber-200 bg-amber-100 text-amber-800",
    },
    ShiftResult.ShiftTypeChoices.NIGHT: {
        "label": "夜",
        "classes": "border-sky-200 bg-sky-100 text-sky-800",
    },
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT: {
        "label": "明",
        "classes": "border-violet-200 bg-violet-100 text-violet-800",
    },
    ShiftResult.ShiftTypeChoices.OFF: {
        "label": "休",
        "classes": "border-rose-200 bg-rose-100 text-rose-800",
    },
    ShiftResult.ShiftTypeChoices.OFF_REQUEST: {
        "label": "希",
        "classes": "border-red-200 bg-red-100 text-red-800",
    },
    "blank": {
        "label": "",
        "classes": "border-slate-200 bg-white text-slate-400",
    },
}

WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]


def get_shift_select_classes(shift_type):  # シフトが決まってなければ空欄とCSSを返し、決まっていれば勤務とCSSを返す
    if shift_type in SHIFT_DISPLAY_CONFIG:
        return SHIFT_DISPLAY_CONFIG[shift_type]["classes"]
    return SHIFT_DISPLAY_CONFIG["blank"]["classes"]


def get_month_dates(year, month):  # 指定した年月の全日付を取得
    last_day = calendar.monthrange(year, month)[1] #(2, 31)みたいに1日が何曜日かと月末日を返す
    return [date(year, month, day) for day in range(1, last_day + 1)]


def build_day_headers(month_dates):  # 日付、曜日、土日判定をまとめて返す
    return [
        {
            "date": current_date,
            "weekday_label": WEEKDAY_LABELS[current_date.weekday()],
            "is_saturday": current_date.weekday() == 5,
            "is_sunday": current_date.weekday() == 6,
        }
        for current_date in month_dates
    ]

    # シフトプランのグリッドを構築
def build_shift_plan_grid(staff_members, month_dates, shift_results_by_key, day_off_request_keys):
    staff_rows = []
    day_totals = {current_date: {"day": 0, "night": 0} for current_date in month_dates}

    for staff_member in staff_members:  # モデルからスタッフの希望休日を取得
        regular_day_offs = set(
            staff_member.regular_days_off.values_list("day_of_week", flat=True)
        )
        row_stats = {"day": 0, "night": 0, "off": 0}
        cells = []

        for current_date in month_dates:  # 各日のシフト情報を取得
            result = shift_results_by_key.get((staff_member.id, current_date))  # IDと日付をキーにして保存済みのシフト情報を取得
            shift_type = None  # 勤務区分なしを定義
            source = "blank"  # 情報源はなしで設定

            if result:  # ①保存済、②希望休、③固定休日の順で勤務区分を決定
                shift_type = result.shift_type
                source = "saved"
            elif (staff_member.id, current_date) in day_off_request_keys:
                shift_type = ShiftResult.ShiftTypeChoices.OFF_REQUEST
                source = "day_off_request"
            elif current_date.weekday() in regular_day_offs:
                shift_type = ShiftResult.ShiftTypeChoices.OFF
                source = "regular_day_off"
            # 集計用処理、日勤、夜勤、休日の数をカウント
            if shift_type == ShiftResult.ShiftTypeChoices.DAY:
                row_stats["day"] += 1
                day_totals[current_date]["day"] += 1
            elif shift_type == ShiftResult.ShiftTypeChoices.NIGHT:
                row_stats["night"] += 1
                day_totals[current_date]["night"] += 1
            elif shift_type in (
                ShiftResult.ShiftTypeChoices.OFF,
                ShiftResult.ShiftTypeChoices.OFF_REQUEST,
            ):
                row_stats["off"] += 1  # 休日の数をカウント
            # シフトセルの表示ラベルとCSSクラスを取得
            config = SHIFT_DISPLAY_CONFIG.get(shift_type, SHIFT_DISPLAY_CONFIG["blank"])
            cells.append(  # 1セル分の情報をまとめてとる
                {
                    "date": current_date,
                    "field_name": f"shift_{staff_member.id}_{current_date.isoformat()}",  # HTMLに渡す用の名前
                    "value": shift_type or "",
                    "display_label": config["label"],
                    "display_classes": config["classes"],
                    "source": source,
                    "is_locked": result.is_locked if result else False,
                }
            )

        staff_rows.append(  # セル情報をスタッフごとの行分のリストのまとめる
            {
                "staff_member": staff_member,
                "cells": cells,
                "stats": row_stats,
            }
        )

    day_summary_rows = [  # 日の日、夜、休日の数を集計をまとめる
        {
            "label": "日別集計",
            "values": [
                {
                    "date": current_date,
                    "day_count": day_totals[current_date]["day"],
                    "night_count": day_totals[current_date]["night"],
                }
                for current_date in month_dates
            ],
        }
    ]

    return staff_rows, day_summary_rows  # スタッフの行情報と日別集計情報を返す


class UserShiftPlanMixin(LoginRequiredMixin):
    def get_queryset(self):  # ユーザーに関連するシフト情報を取得
        return ShiftPlan.objects.filter(user=self.request.user)

    def get_object(self):  # 保存済みのシフト情報を取得、なければ404エラー
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def get_staff_members(self):  # ユーザーに関連するスタッフメンバーを取得
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off").order_by("id")

    def get_shift_rule_form(self, shift_plan, data=None):  # シフトルールフォームを取得
        try:
            shift_rule = shift_plan.shift_rule
        except ShiftPlan.shift_rule.RelatedObjectDoesNotExist:
            shift_rule = None
        return ShiftRuleForm(data=data, shift_rule=shift_rule)

    def get_shift_results_by_key(self, shift_plan, staff_members):  # 保存済のシフト情報を取得、スタッフIDと日付をキーにして辞書で返す
        shift_results = ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).select_related("staff_member")
        return {
            (shift_result.staff_member_id, shift_result.date): shift_result
            for shift_result in shift_results
        }

    def get_day_off_request_keys(self, shift_plan, staff_members):  # 希望休がある日を取得、スタッフIDと日付をキーにしてセットで返す
        day_off_requests = DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
        return {
            (day_off_request.staff_member_id, day_off_request.date)
            for day_off_request in day_off_requests
        }

    def build_context(self, shift_plan, rule_form): # シフト表の表示に必要な情報をまとめて返す
        staff_members = list(self.get_staff_members()) # メンバーを取得
        month_dates = get_month_dates(shift_plan.year, shift_plan.month) # 日付を取得
        day_headers = build_day_headers(month_dates) # 日付のヘッダー部分を作る
        shift_results_by_key = self.get_shift_results_by_key(shift_plan, staff_members) # 保存済のシフト情報を取得
        day_off_request_keys = self.get_day_off_request_keys(shift_plan, staff_members) # 希望休がある日を取得
        staff_rows, day_summary_rows = build_shift_plan_grid( # シフト表の行と日別集計を作成
            staff_members,
            month_dates,
            shift_results_by_key,
            day_off_request_keys,
        )
        return { # HTMLに渡す情報をまとめる
            "shift_plan": shift_plan,
            "rule_form": rule_form,
            "staff_rows": staff_rows,
            "month_dates": month_dates,
            "day_headers": day_headers,
            "shift_select_options": SHIFT_SELECT_OPTIONS,
            "day_summary_rows": day_summary_rows,
            "staff_count": len(staff_members),
        }
    # 画面で選択されたシフトの情報をDBに保存する処理、既存のシフト情報を取得し、選択された値が有効な勤務区分であれば保存、空欄であれば削除する
    def save_shift_results(self, shift_plan, staff_members, month_dates):
        existing_results = self.get_shift_results_by_key(shift_plan, staff_members)
        valid_shift_types = { # 変な勤務区分を送られないよう、有効な勤務区分をセットにしておく
            choice[0]
            for choice in ShiftResult.ShiftTypeChoices.choices
        }

        for staff_member in staff_members: # スタッフ-日付を確認
            for current_date in month_dates:
                field_name = f"shift_{staff_member.id}_{current_date.isoformat()}"
                selected_value = self.request.POST.get(field_name, "").strip() # POSTされた勤務区分を取得
                existing_result = existing_results.get((staff_member.id, current_date)) # 既存のシフト情報を取得

                if not selected_value: # 空欄であればDBから削除
                    if existing_result:
                        existing_result.delete()
                    continue

                if selected_value not in valid_shift_types: # 不正な勤務区分なら無視
                    continue

                if existing_result: # 既存のシフト情報がある場合は更新、なければ新規作成する
                    existing_result.shift_type = selected_value
                    existing_result.input_type = ShiftResult.InputTypeChoices.MANUAL
                    existing_result.save(update_fields=["shift_type", "input_type", "updated_at"])
                    continue

                ShiftResult.objects.create( # 新規作成
                    shift_plan=shift_plan,
                    staff_member=staff_member,
                    date=current_date,
                    shift_type=selected_value,
                    input_type=ShiftResult.InputTypeChoices.MANUAL,
                )


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
        return HttpResponseRedirect(
            reverse("shifts:edit", kwargs={"pk": self.object.pk})
        )


class ShiftPlanEditView(UserShiftPlanMixin, View):
    template_name = "shifts/shift_plan_edit.html"

    def get(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        rule_form = self.get_shift_rule_form(shift_plan)
        context = self.build_context(shift_plan, rule_form)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        action = request.POST.get("action", "save")
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)

        if action == "save_conditions":
            rule_form = self.get_shift_rule_form(shift_plan, data=request.POST)
            if rule_form.is_valid():
                rule_form.save(shift_plan)
                messages.success(request, "シフト条件を保存しました。")
                return HttpResponseRedirect(
                    reverse("shifts:edit", kwargs={"pk": shift_plan.pk})
                )

            context = self.build_context(shift_plan, rule_form)
            return render(request, self.template_name, context)

        if action == "reset":
            ShiftResult.objects.filter(shift_plan=shift_plan).delete()
            messages.success(request, "シフトを初期状態にリセットしました。")
            return HttpResponseRedirect(
                reverse("shifts:edit", kwargs={"pk": shift_plan.pk})
            )

        if action == "generate":
            messages.info(request, "シフト生成処理は未実装です。")
            return HttpResponseRedirect(
                reverse("shifts:edit", kwargs={"pk": shift_plan.pk})
            )

        with transaction.atomic():
            self.save_shift_results(shift_plan, staff_members, month_dates)

            if action == "lock":
                ShiftResult.objects.filter(shift_plan=shift_plan).update(is_locked=True)
                messages.success(request, "現在のシフトを固定しました。")
            else:
                messages.success(request, "シフトを保存しました。")

        return HttpResponseRedirect(
            reverse("shifts:edit", kwargs={"pk": shift_plan.pk})
        )
