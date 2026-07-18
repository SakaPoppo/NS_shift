import calendar
from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView

from staff.models import StaffMember

from .forms import (
    DateShiftRuleForm,
    ShiftPlanCreateForm,
    ShiftRuleForm,
    WeekdayShiftRuleForm,
)
from .models import DayOffRequest, ShiftPlan, ShiftResult, ShiftRule

SHIFT_SELECT_OPTIONS = [ #
    ("", ""),
    (ShiftResult.ShiftTypeChoices.DAY, "日"),
    (ShiftResult.ShiftTypeChoices.NIGHT, "夜"),
    (ShiftResult.ShiftTypeChoices.AFTER_NIGHT, "明"),
    (ShiftResult.ShiftTypeChoices.OFF, "休"),
    (ShiftResult.ShiftTypeChoices.OFF_REQUEST, "希"),
    (ShiftResult.ShiftTypeChoices.PAID_LEAVE, "有"),
    (ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE, "特"),
    (ShiftResult.ShiftTypeChoices.TRAINING, "研"),
]

SHIFT_DISPLAY_CONFIG = {
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
    ShiftResult.ShiftTypeChoices.PAID_LEAVE: {
        "label": "有",
        "classes": "border-pink-200 bg-pink-100 text-pink-800",
    },
    ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE: {
        "label": "特",
        "classes": "border-pink-200 bg-pink-100 text-pink-800",
    },
    ShiftResult.ShiftTypeChoices.TRAINING: {
        "label": "研",
        "classes": "border-emerald-200 bg-emerald-100 text-emerald-800",
    },
    "blank": {
        "label": "",
        "classes": "border-slate-200 bg-white text-slate-400",
    },
}

SHIFT_TYPE_LABELS = dict(ShiftResult.ShiftTypeChoices.choices)
BASE_FIXED_SOURCE_LABELS = {
    "day_off_request": "希望休",
    "regular_day_off": "固定休",
}
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]


def get_month_dates(year, month):
    # year と month を受け取り、その月の date 一覧を返す。
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last_day + 1)]


def build_day_headers(month_dates):
    # date 一覧を受け取り、曜日表示用の辞書リストに変換して返す。
    return [
        {
            "date": current_date,
            "weekday_label": WEEKDAY_LABELS[current_date.weekday()],
            "is_saturday": current_date.weekday() == 5,
            "is_sunday": current_date.weekday() == 6,
        }
        for current_date in month_dates
    ]


def build_shift_plan_grid(
    staff_members,
    month_dates,
    shift_results_by_key,
    base_fixed_assignments,
    display_assignments=None,
):
    # スタッフ一覧と各種シフト情報を受け取り、編集画面表示用の行データと日別集計を返す。
    staff_rows = []
    day_totals = {current_date: {"day": 0, "night": 0} for current_date in month_dates}

    for staff_member in staff_members:
        row_stats = {
            "day": 0,
            "night": 0,
            "off": 0,
            "paid_leave": 0,
            "special_leave": 0,
            "training": 0,
        }
        cells = []

        for current_date in month_dates:
            cell_key = (staff_member.id, current_date)
            result = shift_results_by_key.get(cell_key)
            shift_type = None
            source = "blank"
            is_base_fixed = False
            has_conflict = False
            conflicting_shift_type = None
            conflict_message = ""

            base_fixed = base_fixed_assignments.get(cell_key)
            if base_fixed:
                shift_type = base_fixed["shift_type"]
                source = base_fixed["source"]
                is_base_fixed = True
                has_conflict = result is not None
                conflicting_shift_type = result.shift_type if result else None
                if conflicting_shift_type:
                    conflict_message = (
                        f'保存済みの「{SHIFT_TYPE_LABELS.get(conflicting_shift_type, conflicting_shift_type)}」'
                        "と競合しています。"
                    )
            elif display_assignments is not None and cell_key in display_assignments:
                shift_type = display_assignments[cell_key]
                source = "submitted" if shift_type else "blank"
            elif result:
                shift_type = result.shift_type
                source = "saved"

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
                row_stats["off"] += 1
            elif shift_type == ShiftResult.ShiftTypeChoices.PAID_LEAVE:
                row_stats["paid_leave"] += 1
            elif shift_type == ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE:
                row_stats["special_leave"] += 1
            elif shift_type == ShiftResult.ShiftTypeChoices.TRAINING:
                row_stats["training"] += 1

            config = SHIFT_DISPLAY_CONFIG.get(shift_type, SHIFT_DISPLAY_CONFIG["blank"])
            display_classes = config["classes"]
            if has_conflict:
                display_classes = f"{display_classes} border-orange-400 ring-1 ring-orange-300"
            cells.append(
                {
                    "date": current_date,
                    "field_name": f"shift_{staff_member.id}_{current_date.isoformat()}",
                    "value": shift_type or "",
                    "display_label": config["label"],
                    "display_classes": display_classes,
                    "source": source,
                    "is_base_fixed": is_base_fixed,
                    "has_conflict": has_conflict,
                    "conflicting_shift_type": conflicting_shift_type,
                    "conflict_message": conflict_message,
                }
            )

        staff_rows.append(
            {
                "staff_member": staff_member,
                "cells": cells,
                "stats": row_stats,
            }
        )

    day_summary_rows = [
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

    return staff_rows, day_summary_rows


class UserShiftPlanMixin(LoginRequiredMixin):
    """ログイン中ユーザーのシフト表操作で共通利用するMixin。

    受け取るもの:
    - request.user
    - 各 view から渡される shift_plan や POST データ

    返すもの:
    - シフト表取得用 queryset
    - 条件画面、編集画面で使うフォームや context
    """

    condition_success_message = "シフト条件を保存しました。"

    def get_queryset(self):
        # request.user を使って、そのユーザーの ShiftPlan queryset を返す。
        return ShiftPlan.objects.filter(user=self.request.user)

    def get_object(self):
        # URL の pk を受け取り、ログインユーザー所有の ShiftPlan を1件返す。
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def get_shift_rule(self, shift_plan):
        # ShiftPlan を受け取り、紐づく ShiftRule を返す。未作成なら None を返す。
        try:
            return shift_plan.shift_rule
        except ShiftRule.DoesNotExist:
            return None

    def get_staff_members(self):
        # ログインユーザーの有効なスタッフ一覧を返す。
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off").order_by("id")

    def get_shift_rule_form(self, shift_plan, data=None):
        # ShiftPlan と POST データを受け取り、月共通ルール用フォームを返す。
        return ShiftRuleForm(data=data, shift_rule=self.get_shift_rule(shift_plan))

    def get_conditions_url(self, shift_plan):
        # ShiftPlan を受け取り、条件設定画面のURL文字列を返す。
        return reverse("shifts:conditions", kwargs={"pk": shift_plan.pk})

    def get_edit_url(self, shift_plan):
        # ShiftPlan を受け取り、シフト編集画面のURL文字列を返す。
        return reverse("shifts:edit", kwargs={"pk": shift_plan.pk})

    def get_ordered_date_rules(self, shift_plan):
        # ShiftPlan を受け取り、日付順に並べた特定日条件 queryset を返す。
        return shift_plan.date_rules.order_by("target_date", "id")

    def get_weekday_forms(self, shift_plan, data=None):
        # ShiftPlan と POST データを受け取り、月〜日の曜日条件フォーム一覧を返す。
        weekday_rules = {
            weekday_rule.day_of_week: weekday_rule
            for weekday_rule in shift_plan.weekday_rules.all()
        }
        return [
            WeekdayShiftRuleForm(
                data=data,
                prefix=f"weekday-{day_of_week}",
                day_of_week=day_of_week,
                instance=weekday_rules.get(day_of_week),
            )
            for day_of_week in range(7)
        ]

    def get_date_rule_forms(self, shift_plan, data=None):
        # ShiftPlan と POST データを受け取り、特定日条件フォーム一覧を返す。
        if data is not None:
            try:
                total_forms = int(data.get("date_rule_total_forms", 0))
            except (TypeError, ValueError):
                total_forms = 0
            forms = []
            for index in range(max(total_forms, 0)):
                prefix = f"date-rule-{index}"
                date_rule_id = data.get(f"{prefix}-date_rule_id")
                instance = None
                if date_rule_id:
                    instance = get_object_or_404(shift_plan.date_rules.all(), pk=date_rule_id)
                forms.append(
                    DateShiftRuleForm(
                        data=data,
                        prefix=prefix,
                        shift_plan=shift_plan,
                        instance=instance,
                    )
                )
            return forms

        return [
            DateShiftRuleForm(
                prefix=f"date-rule-{index}",
                shift_plan=shift_plan,
                instance=date_rule,
            )
            for index, date_rule in enumerate(self.get_ordered_date_rules(shift_plan))
        ]

    def get_empty_date_rule_form(self, shift_plan):
        # 新規追加用の空の特定日フォームを1件返す。
        return DateShiftRuleForm(
            prefix="date-rule-__prefix__",
            shift_plan=shift_plan,
        )

    def get_shift_results_by_key(self, shift_plan, staff_members):
        # ShiftPlan とスタッフ一覧を受け取り、(staff_id, date) をキーにした ShiftResult 辞書を返す。
        shift_results = ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).select_related("staff_member")
        return {
            (shift_result.staff_member_id, shift_result.date): shift_result
            for shift_result in shift_results
        }

    def get_base_fixed_assignments(self, shift_plan, staff_members, month_dates):
        # 希望休と固定休を受け取り、表示と保存で共通利用する基礎データ辞書を返す。
        month_dates_set = set(month_dates)
        base_fixed_assignments = {}
        day_off_requests = DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
            date__in=month_dates_set,
        )
        for day_off_request in day_off_requests:
            base_fixed_assignments[(day_off_request.staff_member_id, day_off_request.date)] = {
                "shift_type": ShiftResult.ShiftTypeChoices.OFF_REQUEST,
                "source": "day_off_request",
            }

        for staff_member in staff_members:
            regular_days_off = set(
                staff_member.regular_days_off.values_list("day_of_week", flat=True)
            )
            for current_date in month_dates:
                cell_key = (staff_member.id, current_date)
                if cell_key in base_fixed_assignments:
                    continue
                if current_date.weekday() in regular_days_off:
                    base_fixed_assignments[cell_key] = {
                        "shift_type": ShiftResult.ShiftTypeChoices.OFF,
                        "source": "regular_day_off",
                    }

        return base_fixed_assignments

    def build_edit_context(self, shift_plan, *, display_assignments=None):
        # ShiftPlan を受け取り、シフト編集画面描画に必要な context をまとめて返す。
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)
        day_headers = build_day_headers(month_dates)
        shift_results_by_key = self.get_shift_results_by_key(shift_plan, staff_members)
        base_fixed_assignments = self.get_base_fixed_assignments(
            shift_plan,
            staff_members,
            month_dates,
        )
        staff_rows, day_summary_rows = build_shift_plan_grid(
            staff_members,
            month_dates,
            shift_results_by_key,
            base_fixed_assignments,
            display_assignments=display_assignments,
        )
        shift_rule = self.get_shift_rule(shift_plan)
        return {
            "shift_plan": shift_plan,
            "shift_rule": shift_rule,
            "staff_rows": staff_rows,
            "month_dates": month_dates,
            "day_headers": day_headers,
            "shift_select_options": SHIFT_SELECT_OPTIONS,
            "day_summary_rows": day_summary_rows,
            "staff_count": len(staff_members),
            "weekday_rule_count": shift_plan.weekday_rules.count(),
            "date_rule_count": shift_plan.date_rules.count(),
        }

    def parse_submitted_assignments(
        self,
        shift_plan,
        staff_members,
        month_dates,
        base_fixed_assignments,
    ):
        # POSTされた勤務入力を受け取り、編集可能セルだけの勤務辞書を返す。
        valid_shift_types = {
            choice[0]
            for choice in ShiftResult.ShiftTypeChoices.choices
        }
        submitted_assignments = {}

        for staff_member in staff_members:
            for current_date in month_dates:
                cell_key = (staff_member.id, current_date)
                if cell_key in base_fixed_assignments:
                    continue

                field_name = f"shift_{staff_member.id}_{current_date.isoformat()}"
                if field_name not in self.request.POST:
                    continue
                selected_value = self.request.POST.get(field_name, "").strip()
                if selected_value and selected_value not in valid_shift_types:
                    selected_value = ""
                submitted_assignments[cell_key] = selected_value

        return submitted_assignments

    def build_final_shift_types(
        self,
        staff_members,
        month_dates,
        existing_results_by_key,
        base_fixed_assignments,
        submitted_assignments,
    ):
        # 画面保存後に成立する最終勤務状態を、(staff_id, date) -> shift_type で返す。
        final_shift_types = {}

        for staff_member in staff_members:
            for current_date in month_dates:
                cell_key = (staff_member.id, current_date)
                if cell_key in base_fixed_assignments:
                    final_shift_types[cell_key] = base_fixed_assignments[cell_key]["shift_type"]
                    continue

                if cell_key in submitted_assignments:
                    final_shift_types[cell_key] = submitted_assignments[cell_key]
                    continue

                existing_result = existing_results_by_key.get(cell_key)
                final_shift_types[cell_key] = existing_result.shift_type if existing_result else ""

        return final_shift_types

    def validate_manual_assignments(
        self,
        staff_members,
        month_dates,
        submitted_assignments,
        base_fixed_assignments,
        existing_results_by_key,
    ):
        # 保存前の勤務入力を検証し、問題があればエラーメッセージ一覧を返す。
        month_dates_set = set(month_dates)
        final_shift_types = self.build_final_shift_types(
            staff_members,
            month_dates,
            existing_results_by_key,
            base_fixed_assignments,
            submitted_assignments,
        )
        changed_keys = set()

        for cell_key, selected_value in submitted_assignments.items():
            existing_result = existing_results_by_key.get(cell_key)
            existing_value = existing_result.shift_type if existing_result else ""
            if selected_value != existing_value:
                changed_keys.add(cell_key)

        validation_targets = set(changed_keys)
        for staff_member_id, current_date in changed_keys:
            for offset in (-1, 1):
                adjacent_date = current_date.fromordinal(current_date.toordinal() + offset)
                if adjacent_date in month_dates_set:
                    validation_targets.add((staff_member_id, adjacent_date))

        errors = []
        for staff_member_id, current_date in sorted(validation_targets, key=lambda item: (item[0], item[1])):
            current_key = (staff_member_id, current_date)
            current_shift_type = final_shift_types.get(current_key, "")

            if current_shift_type == ShiftResult.ShiftTypeChoices.NIGHT:
                next_date = current_date.fromordinal(current_date.toordinal() + 1)
                if next_date not in month_dates_set:
                    continue

                next_key = (staff_member_id, next_date)
                base_fixed = base_fixed_assignments.get(next_key)
                if base_fixed:
                    source_label = BASE_FIXED_SOURCE_LABELS[base_fixed["source"]]
                    errors.append(
                        f"{current_date.day}日の夜勤は保存できません。"
                        f"翌日の{next_date.day}日が{source_label}のため、夜勤明けを配置できません。"
                    )
                    continue

                next_shift_type = final_shift_types.get(next_key, "")
                if next_shift_type and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
                    errors.append(
                        f"{current_date.day}日の夜勤は保存できません。"
                        f"翌日の{next_date.day}日に「{SHIFT_TYPE_LABELS.get(next_shift_type, next_shift_type)}」"
                        "が入っているため、夜勤明けを配置できません。"
                    )

            if current_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
                previous_date = current_date.fromordinal(current_date.toordinal() - 1)
                if previous_date not in month_dates_set:
                    errors.append(
                        f"{current_date.day}日の明けは保存できません。"
                        f"前日の{previous_date.day}日に夜勤が存在しません。"
                    )
                    continue

                previous_key = (staff_member_id, previous_date)
                base_fixed = base_fixed_assignments.get(previous_key)
                if base_fixed:
                    source_label = BASE_FIXED_SOURCE_LABELS[base_fixed["source"]]
                    errors.append(
                        f"{current_date.day}日の明けは保存できません。"
                        f"前日の{previous_date.day}日が{source_label}のため、夜勤が存在しません。"
                    )
                    continue

                previous_shift_type = final_shift_types.get(previous_key, "")
                if previous_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                    if previous_shift_type:
                        errors.append(
                            f"{current_date.day}日の明けは保存できません。"
                            f"前日の{previous_date.day}日に「{SHIFT_TYPE_LABELS.get(previous_shift_type, previous_shift_type)}」"
                            "が入っているため、夜勤の翌日になっていません。"
                        )
                    else:
                        errors.append(
                            f"{current_date.day}日の明けは保存できません。"
                            f"前日の{previous_date.day}日に夜勤が存在しません。"
                        )

        deduped_errors = []
        seen = set()
        for error in errors:
            if error in seen:
                continue
            seen.add(error)
            deduped_errors.append(error)
        return deduped_errors

    def build_condition_context(
        self,
        shift_plan,
        rule_form,
        weekday_forms,
        date_rule_forms,
        empty_date_rule_form,
    ):
        # 条件設定画面で使うフォーム群を受け取り、template 用 context を返す。
        return {
            "shift_plan": shift_plan,
            "rule_form": rule_form,
            "weekday_form_rows": [
                {
                    "label": WEEKDAY_LABELS[index],
                    "form": form,
                    "is_selected": form.is_selected(),
                }
                for index, form in enumerate(weekday_forms)
            ],
            "date_rule_forms": date_rule_forms,
            "empty_date_rule_form": empty_date_rule_form,
            "date_rule_total_forms": len(date_rule_forms),
        }

    def save_manual_shift_results(
        self,
        shift_plan,
        submitted_assignments,
        existing_results_by_key,
    ):
        # 検証済みの勤務入力を受け取り、必要なセルだけ MANUAL として保存する。
        for (staff_member_id, current_date), selected_value in submitted_assignments.items():
            existing_result = existing_results_by_key.get((staff_member_id, current_date))

            if not selected_value:
                if existing_result:
                    existing_result.delete()
                continue

            if existing_result:
                if selected_value == existing_result.shift_type:
                    continue
                existing_result.shift_type = selected_value
                existing_result.input_type = ShiftResult.InputTypeChoices.MANUAL
                existing_result.save(update_fields=["shift_type", "input_type", "updated_at"])
                continue

            ShiftResult.objects.create(
                shift_plan=shift_plan,
                staff_member_id=staff_member_id,
                date=current_date,
                shift_type=selected_value,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

    def reset_generated_results(self, shift_plan):
        # 自動生成勤務だけを削除して、手入力勤務は残す。
        ShiftResult.objects.filter(
            shift_plan=shift_plan,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        ).delete()

    def reset_all_shift_results(self, shift_plan):
        # 勤務結果を全削除して、希望休・固定休だけの状態へ戻す。
        ShiftResult.objects.filter(
            shift_plan=shift_plan,
        ).delete()


class ShiftPlanListView(LoginRequiredMixin, ListView):
    # ログインユーザーが作成したシフト表一覧を表示するビュー。
    model = ShiftPlan
    template_name = "shifts/shift_plan_list.html"
    context_object_name = "shift_plans"

    def get_queryset(self):
        # request.user を使って、自分のシフト表一覧 queryset を返す。
        return ShiftPlan.objects.filter(user=self.request.user)


class ShiftPlanCreateView(LoginRequiredMixin, CreateView):
    # シフト表の基本情報を受け取り、新規作成後に条件設定画面へ遷移するビュー。
    model = ShiftPlan
    form_class = ShiftPlanCreateForm
    template_name = "shifts/shift_plan_create.html"
    success_url = reverse_lazy("shifts:list")

    def get_form_kwargs(self):
        # フォームに request.user を渡すための kwargs を返す。
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        # 入力済みフォームを受け取り、user をセットして保存し、条件設定画面へリダイレクトする。
        form.instance.user = self.request.user
        self.object = form.save()
        return HttpResponseRedirect(
            reverse("shifts:conditions", kwargs={"pk": self.object.pk})
        )


class ShiftRuleEditView(UserShiftPlanMixin, View):
    # 月共通条件・曜日条件・特定日条件をまとめて編集するビュー。
    template_name = "shifts/shift_rule_form.html"

    def get(self, request, *args, **kwargs):
        # URL の pk を受け取り、条件設定画面表示用の context を返す。
        shift_plan = self.get_object()
        context = self.build_condition_context(
            shift_plan=shift_plan,
            rule_form=self.get_shift_rule_form(shift_plan),
            weekday_forms=self.get_weekday_forms(shift_plan),
            date_rule_forms=self.get_date_rule_forms(shift_plan),
            empty_date_rule_form=self.get_empty_date_rule_form(shift_plan),
        )
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        # 条件入力のPOSTを受け取り、削除または保存を行って次画面へ返す。
        shift_plan = self.get_object()
        delete_date_rule_id = request.POST.get("delete_date_rule_id")

        if delete_date_rule_id:
            date_rule = get_object_or_404(
                shift_plan.date_rules.all(),
                pk=delete_date_rule_id,
            )
            date_rule.delete()
            messages.success(request, "特定日条件を削除しました。")
            return HttpResponseRedirect(self.get_conditions_url(shift_plan))

        rule_form = self.get_shift_rule_form(shift_plan, data=request.POST)
        weekday_forms = self.get_weekday_forms(shift_plan, data=request.POST)
        date_rule_forms = self.get_date_rule_forms(shift_plan, data=request.POST)

        rule_form_is_valid = rule_form.is_valid()
        weekday_forms_are_valid = all(form.is_valid() for form in weekday_forms)
        date_rule_forms_are_valid = all(form.is_valid() for form in date_rule_forms)
        forms_are_valid = all(
            (
                rule_form_is_valid,
                weekday_forms_are_valid,
                date_rule_forms_are_valid,
            )
        )

        if not forms_are_valid:
            context = self.build_condition_context(
                shift_plan=shift_plan,
                rule_form=rule_form,
                weekday_forms=weekday_forms,
                date_rule_forms=date_rule_forms,
                empty_date_rule_form=self.get_empty_date_rule_form(shift_plan),
            )
            return render(request, self.template_name, context)

        with transaction.atomic():
            rule_form.save(shift_plan)
            for weekday_form in weekday_forms:
                weekday_form.save(shift_plan)
            for date_rule_form in date_rule_forms:
                date_rule_form.save(shift_plan)

        messages.success(request, self.condition_success_message)
        return HttpResponseRedirect(self.get_edit_url(shift_plan))


class ShiftPlanEditView(UserShiftPlanMixin, View):
    # シフト表本体の手動入力・保存・リセットを行うビュー。
    template_name = "shifts/shift_plan_edit.html"

    def get(self, request, *args, **kwargs):
        # ShiftPlan を受け取り、条件未設定なら条件画面へ、設定済みなら編集画面を返す。
        shift_plan = self.get_object()
        if self.get_shift_rule(shift_plan) is None:
            return redirect("shifts:conditions", pk=shift_plan.pk)

        context = self.build_edit_context(shift_plan)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        # action を受け取り、シフト保存・リセットなどの処理結果を返す。
        shift_plan = self.get_object()
        if self.get_shift_rule(shift_plan) is None:
            return redirect("shifts:conditions", pk=shift_plan.pk)

        action = request.POST.get("action", "save")
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)
        existing_results_by_key = self.get_shift_results_by_key(shift_plan, staff_members)
        base_fixed_assignments = self.get_base_fixed_assignments(
            shift_plan,
            staff_members,
            month_dates,
        )

        if action == "reset_to_manual":
            self.reset_generated_results(shift_plan)
            messages.success(request, "自動生成した勤務を削除し、手入力した勤務だけを残しました。")
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        if action == "reset_to_base":
            self.reset_all_shift_results(shift_plan)
            messages.success(request, "勤務データを削除し、希望休・固定休だけの状態へ戻しました。")
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        if action == "generate":
            messages.info(request, "シフト生成処理は未実装です。")
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        submitted_assignments = self.parse_submitted_assignments(
            shift_plan,
            staff_members,
            month_dates,
            base_fixed_assignments,
        )
        validation_errors = self.validate_manual_assignments(
            staff_members,
            month_dates,
            submitted_assignments,
            base_fixed_assignments,
            existing_results_by_key,
        )
        if validation_errors:
            for error in validation_errors:
                messages.error(request, error)
            context = self.build_edit_context(
                shift_plan,
                display_assignments=submitted_assignments,
            )
            return render(request, self.template_name, context)

        with transaction.atomic():
            self.save_manual_shift_results(
                shift_plan,
                submitted_assignments,
                existing_results_by_key,
            )
            messages.success(request, "シフトを保存しました。")

        return HttpResponseRedirect(self.get_edit_url(shift_plan))


class ShiftPlanDeleteView(UserShiftPlanMixin, View):
    # シフト表削除の確認画面と削除実行を担当するビュー。
    template_name = "shifts/shift_plan_confirm_delete.html"

    def get(self, request, *args, **kwargs):
        # 削除対象の ShiftPlan を受け取り、確認画面を返す。
        shift_plan = self.get_object()
        return render(
            request,
            self.template_name,
            {
                "shift_plan": shift_plan,
            },
        )

    def post(self, request, *args, **kwargs):
        # 削除対象の ShiftPlan を受け取り、削除後に一覧へリダイレクトする。
        shift_plan = self.get_object()
        shift_plan.delete()
        messages.success(request, "シフト表を削除しました。")
        return HttpResponseRedirect(reverse("shifts:list"))
