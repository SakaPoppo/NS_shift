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
from .models import DateShiftRule, DayOffRequest, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule

SHIFT_SELECT_OPTIONS = [
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

WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]


def get_month_dates(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last_day + 1)]


def build_day_headers(month_dates):
    return [
        {
            "date": current_date,
            "weekday_label": WEEKDAY_LABELS[current_date.weekday()],
            "is_saturday": current_date.weekday() == 5,
            "is_sunday": current_date.weekday() == 6,
        }
        for current_date in month_dates
    ]


def build_shift_plan_grid(staff_members, month_dates, shift_results_by_key, day_off_request_keys):
    staff_rows = []
    day_totals = {current_date: {"day": 0, "night": 0} for current_date in month_dates}

    for staff_member in staff_members:
        regular_day_offs = set(
            staff_member.regular_days_off.values_list("day_of_week", flat=True)
        )
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
            result = shift_results_by_key.get((staff_member.id, current_date))
            shift_type = None
            source = "blank"

            if result:
                shift_type = result.shift_type
                source = "saved"
            elif (staff_member.id, current_date) in day_off_request_keys:
                shift_type = ShiftResult.ShiftTypeChoices.OFF_REQUEST
                source = "day_off_request"
            elif current_date.weekday() in regular_day_offs:
                shift_type = ShiftResult.ShiftTypeChoices.OFF
                source = "regular_day_off"

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
            cells.append(
                {
                    "date": current_date,
                    "field_name": f"shift_{staff_member.id}_{current_date.isoformat()}",
                    "value": shift_type or "",
                    "display_label": config["label"],
                    "display_classes": config["classes"],
                    "source": source,
                    "is_locked": result.is_locked if result else False,
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
    def get_queryset(self):
        return ShiftPlan.objects.filter(user=self.request.user)

    def get_object(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def get_shift_rule(self, shift_plan):
        try:
            return shift_plan.shift_rule
        except ShiftRule.DoesNotExist:
            return None

    def get_staff_members(self):
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off").order_by("id")

    def get_shift_rule_form(self, shift_plan, data=None):
        return ShiftRuleForm(data=data, shift_rule=self.get_shift_rule(shift_plan))

    def get_weekday_forms(self, shift_plan, data=None):
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
            for index, date_rule in enumerate(shift_plan.date_rules.order_by("target_date", "id"))
        ]

    def get_empty_date_rule_form(self, shift_plan):
        return DateShiftRuleForm(
            prefix="date-rule-__prefix__",
            shift_plan=shift_plan,
        )

    def get_shift_results_by_key(self, shift_plan, staff_members):
        shift_results = ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).select_related("staff_member")
        return {
            (shift_result.staff_member_id, shift_result.date): shift_result
            for shift_result in shift_results
        }

    def get_day_off_request_keys(self, shift_plan, staff_members):
        day_off_requests = DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
        return {
            (day_off_request.staff_member_id, day_off_request.date)
            for day_off_request in day_off_requests
        }

    def build_edit_context(self, shift_plan):
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)
        day_headers = build_day_headers(month_dates)
        shift_results_by_key = self.get_shift_results_by_key(shift_plan, staff_members)
        day_off_request_keys = self.get_day_off_request_keys(shift_plan, staff_members)
        staff_rows, day_summary_rows = build_shift_plan_grid(
            staff_members,
            month_dates,
            shift_results_by_key,
            day_off_request_keys,
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

    def build_condition_context(
        self,
        shift_plan,
        rule_form,
        weekday_forms,
        date_rule_forms,
        empty_date_rule_form,
    ):
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

    def save_shift_results(self, shift_plan, staff_members, month_dates):
        existing_results = self.get_shift_results_by_key(shift_plan, staff_members)
        valid_shift_types = {
            choice[0]
            for choice in ShiftResult.ShiftTypeChoices.choices
        }

        for staff_member in staff_members:
            for current_date in month_dates:
                field_name = f"shift_{staff_member.id}_{current_date.isoformat()}"
                selected_value = self.request.POST.get(field_name, "").strip()
                existing_result = existing_results.get((staff_member.id, current_date))

                if not selected_value:
                    if existing_result:
                        existing_result.delete()
                    continue

                if selected_value not in valid_shift_types:
                    continue

                if existing_result:
                    existing_result.shift_type = selected_value
                    existing_result.input_type = ShiftResult.InputTypeChoices.MANUAL
                    existing_result.save(update_fields=["shift_type", "input_type", "updated_at"])
                    continue

                ShiftResult.objects.create(
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
            reverse("shifts:conditions", kwargs={"pk": self.object.pk})
        )


class ShiftRuleEditView(UserShiftPlanMixin, View):
    template_name = "shifts/shift_rule_form.html"

    def get(self, request, *args, **kwargs):
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
        shift_plan = self.get_object()
        delete_date_rule_id = request.POST.get("delete_date_rule_id")

        if delete_date_rule_id:
            date_rule = get_object_or_404(
                shift_plan.date_rules.all(),
                pk=delete_date_rule_id,
            )
            date_rule.delete()
            messages.success(request, "特定日条件を削除しました。")
            return HttpResponseRedirect(
                reverse("shifts:conditions", kwargs={"pk": shift_plan.pk})
            )

        rule_form = self.get_shift_rule_form(shift_plan, data=request.POST)
        weekday_forms = self.get_weekday_forms(shift_plan, data=request.POST)
        date_rule_forms = self.get_date_rule_forms(shift_plan, data=request.POST)

        forms_are_valid = rule_form.is_valid()
        forms_are_valid = all(form.is_valid() for form in weekday_forms) and forms_are_valid
        forms_are_valid = all(form.is_valid() for form in date_rule_forms) and forms_are_valid

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

        messages.success(request, "シフト条件を保存しました。")
        return HttpResponseRedirect(
            reverse("shifts:edit", kwargs={"pk": shift_plan.pk})
        )


class ShiftPlanEditView(UserShiftPlanMixin, View):
    template_name = "shifts/shift_plan_edit.html"

    def get(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        if self.get_shift_rule(shift_plan) is None:
            return redirect("shifts:conditions", pk=shift_plan.pk)

        context = self.build_edit_context(shift_plan)
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        if self.get_shift_rule(shift_plan) is None:
            return redirect("shifts:conditions", pk=shift_plan.pk)

        action = request.POST.get("action", "save")
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)

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


class ShiftPlanDeleteView(UserShiftPlanMixin, View):
    template_name = "shifts/shift_plan_confirm_delete.html"

    def get(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        return render(
            request,
            self.template_name,
            {
                "shift_plan": shift_plan,
            },
        )

    def post(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        shift_plan.delete()
        messages.success(request, "シフト表を削除しました。")
        return HttpResponseRedirect(reverse("shifts:list"))
