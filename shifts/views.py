import csv

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse, HttpResponseRedirect
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
from .models import DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult, ShiftRule
from .services import (
    MonthBoundaryConflictError,
    build_shift_carryovers,
    get_japanese_holiday_dates,
    get_month_dates,
    get_previous_month_year_and_month,
    sync_month_boundary_assignments,
    sync_next_month_boundary_assignments,
)
from .shift_generator import (
    ShiftGenerationError,
    format_generation_violation_messages,
    generate_and_save_shift,
)

SHIFT_SELECT_OPTIONS = [
    ("", "-"),
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
        "label": "-",
        "classes": "border-slate-200 bg-white text-slate-400",
    },
}

SHIFT_TYPE_LABELS = dict(ShiftResult.ShiftTypeChoices.choices)
BASE_FIXED_SOURCE_LABELS = {
    "day_off_request": "希望休",
    "regular_day_off": "曜日固定休",
    "holiday_off": "祝日固定休",
    "month_boundary": "前月勤務の引き継ぎ",
}
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]


def build_day_headers(month_dates, holiday_dates=frozenset()):
    return [
        {
            "date": current_date,
            "weekday_label": WEEKDAY_LABELS[current_date.weekday()],
            "is_saturday": current_date.weekday() == 5,
            "is_sunday": current_date.weekday() == 6,
            "is_holiday": current_date in holiday_dates,
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
    """編集画面用のグリッドデータを組み立てる。

    セルの表示優先順位は次の通り。
    1. 希望休・固定休
    2. バリデーションエラー後に再表示する POST 値
    3. DB に保存済みの ShiftResult
    4. 空欄

    希望休・固定休と保存済み勤務が競合している場合は、基礎データ側を表示しつつ
    競合中の勤務区分を補足情報として残す。
    """
    staff_rows = []
    day_totals = {
        current_date: {
            "day": 0,
            "night": 0,
            "day_ability_total": 0,
            "night_ability_total": 0,
        }
        for current_date in month_dates
    }

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
                has_conflict = result is not None and result.shift_type != shift_type
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
                day_totals[current_date]["day_ability_total"] += staff_member.ability_level
            elif shift_type == ShiftResult.ShiftTypeChoices.NIGHT:
                row_stats["night"] += 1
                day_totals[current_date]["night"] += 1
                day_totals[current_date]["night_ability_total"] += staff_member.ability_level
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
                    "day_ability_total": day_totals[current_date]["day_ability_total"],
                    "night_ability_total": day_totals[current_date]["night_ability_total"],
                }
                for current_date in month_dates
            ],
        }
    ]

    return staff_rows, day_summary_rows


class UserShiftPlanMixin(LoginRequiredMixin):
    """ログイン中ユーザーのシフト表編集で共有する補助処理。"""

    condition_success_message = "シフト条件を保存しました。"

    def get_queryset(self):
        return ShiftPlan.objects.filter(user=self.request.user)

    def get_object(self):
        return get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])

    def get_shift_rule(self, shift_plan):
        try:
            return shift_plan.shift_rule
        except ShiftRule.DoesNotExist:
            return None

    def get_previous_plan_with_conditions(self, shift_plan):
        year, month = get_previous_month_year_and_month(shift_plan.year, shift_plan.month)
        return ShiftPlan.objects.filter(
            user=shift_plan.user, year=year, month=month, shift_rule__isnull=False
        ).select_related("shift_rule").prefetch_related("weekday_rules").first()

    def get_staff_members(self):
        return StaffMember.objects.filter(
            user=self.request.user,
            is_active=True,
        ).prefetch_related("regular_days_off").order_by("id")

    def get_shift_rule_form(self, shift_plan, data=None):
        current_rule = self.get_shift_rule(shift_plan)
        previous_plan = (
            self.get_previous_plan_with_conditions(shift_plan)
            if current_rule is None and data is None else None
        )
        previous_rule = previous_plan.shift_rule if previous_plan else None
        return ShiftRuleForm(
            data=data, shift_rule=current_rule, initial_shift_rule=previous_rule
        )

    def get_conditions_url(self, shift_plan):
        return reverse("shifts:conditions", kwargs={"pk": shift_plan.pk})

    def get_edit_url(self, shift_plan):
        return reverse("shifts:edit", kwargs={"pk": shift_plan.pk})

    def get_ordered_date_rules(self, shift_plan):
        return shift_plan.date_rules.order_by("target_date", "id")

    def get_weekday_forms(self, shift_plan, data=None):
        weekday_rules = {
            weekday_rule.day_of_week: weekday_rule
            for weekday_rule in shift_plan.weekday_rules.all()
        }
        previous_weekday_rules = {}
        if self.get_shift_rule(shift_plan) is None and data is None:
            previous_plan = self.get_previous_plan_with_conditions(shift_plan)
            if previous_plan:
                previous_weekday_rules = {
                    rule.day_of_week: rule for rule in previous_plan.weekday_rules.all()
                }
        return [
            WeekdayShiftRuleForm(
                data=data,
                prefix=f"weekday-{day_of_week}",
                day_of_week=day_of_week,
                instance=weekday_rules.get(day_of_week),
                initial_rule=previous_weekday_rules.get(day_of_week),
            )
            for day_of_week in range(7)
        ]

    def get_date_rule_forms(self, shift_plan, data=None):
        """特定日条件フォームを返す。

        GET 時は既存レコードを日付順で並べ、POST 後は hidden の date_rule_id を使って
        既存レコードとの対応を復元する。
        """
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

    def get_base_fixed_assignments(self, shift_plan, staff_members, month_dates):
        """希望休と曜日・祝日固定休を、編集不可の基礎データへ変換する。

        優先順位は「希望休 > 曜日固定休・祝日固定休」。
        戻り値は {(staff_member_id, date): {"shift_type": str, "source": str}} の辞書で、
        表示時と保存時の両方で同じ判定結果を使う。
        """
        month_dates_set = set(month_dates)
        base_fixed_assignments = {}
        holiday_dates = get_japanese_holiday_dates(shift_plan.year, shift_plan.month)
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
            regular_days_off = {
                day_off.day_of_week for day_off in staff_member.regular_days_off.all()
            }
            for current_date in month_dates:
                cell_key = (staff_member.id, current_date)
                if cell_key in base_fixed_assignments:
                    continue
                if current_date.weekday() in regular_days_off:
                    base_fixed_assignments[cell_key] = {
                        "shift_type": ShiftResult.ShiftTypeChoices.OFF,
                        "source": "regular_day_off",
                    }
                elif staff_member.is_holiday_off and current_date in holiday_dates:
                    base_fixed_assignments[cell_key] = {
                        "shift_type": ShiftResult.ShiftTypeChoices.OFF,
                        "source": "holiday_off",
                    }

        for result in ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
            lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY,
        ):
            cell_key = (result.staff_member_id, result.date)
            if cell_key not in base_fixed_assignments:
                base_fixed_assignments[cell_key] = {
                    "shift_type": result.shift_type,
                    "source": "month_boundary",
                }

        return base_fixed_assignments

    def build_edit_context(self, shift_plan, *, display_assignments=None):
        staff_members = list(self.get_staff_members())
        month_dates = get_month_dates(shift_plan.year, shift_plan.month)
        holiday_dates = get_japanese_holiday_dates(shift_plan.year, shift_plan.month)
        day_headers = build_day_headers(month_dates, holiday_dates)
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
        carryover_staff_ids = set(
            shift_plan.carryovers.filter(
                staff_member__in=staff_members,
                source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            )
            .values_list("staff_member_id", flat=True)
        )
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
            "can_export_csv": shift_plan.status in {
                ShiftPlan.StatusChoices.GENERATED,
                ShiftPlan.StatusChoices.CONFIRMED,
            },
            "missing_previous_staff_members": [
                staff_member for staff_member in staff_members
                if staff_member.id not in carryover_staff_ids
            ],
        }

    def parse_submitted_assignments(
        self,
        shift_plan,
        staff_members,
        month_dates,
        base_fixed_assignments,
    ):
        """POST された勤務入力を、保存候補の辞書へ変換する。

        希望休・固定休セルは HTML だけでなくサーバー側でも除外し、POST されても無視する。
        """
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
        """保存後に成立する勤務状態を、検証用に仮組みする。"""
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
        shift_plan,
        staff_members,
        month_dates,
        submitted_assignments,
        base_fixed_assignments,
        existing_results_by_key,
    ):
        """夜勤と明けの前後関係を検証する。

        夜勤・明けは隣接日とセットで成立するため、変更セルだけでなく前後の日付も検証対象に加える。
        たとえば夜勤を消すと翌日の明けが不正になり、明けを別勤務へ変えると前日の夜勤が不正になる。
        """
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
            # 隣接日との組み合わせで成立する業務ルールなので、前後1日も再検証する。
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
                    # 月末夜勤は翌月の明けをこの画面では検証しない。
                    # TODO: 月をまたぐ「夜勤→明け」の扱いを決定する。
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
                    # 前月情報がないスタッフは、月初の明けを手入力で補完できる。
                    if not ShiftCarryover.objects.filter(
                        shift_plan=shift_plan,
                        staff_member_id=staff_member_id,
                        source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
                    ).exists():
                        continue
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
        """検証済みの入力だけを ShiftResult へ反映する。

        既存の GENERATED 結果と同じ値が送られた場合は、保存ボタンを押しただけで
        MANUAL に昇格しないよう更新を行わない。
        """
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
        """自動生成勤務だけを削除し、手入力勤務は残す。"""
        ShiftResult.objects.filter(
            shift_plan=shift_plan,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=False,
        ).delete()

    def reset_all_shift_results(self, shift_plan):
        """ShiftResult を全削除し、希望休・固定休だけの状態へ戻す。"""
        ShiftResult.objects.filter(
            shift_plan=shift_plan,
        ).exclude(lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY).delete()


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
        build_shift_carryovers(self.object)
        try:
            sync_month_boundary_assignments(self.object)
        except MonthBoundaryConflictError:
            # 条件未設定・月初競合は編集画面で確認できるよう、作成自体は完了させる。
            pass
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
        existing_results_by_key = self.get_shift_results_by_key(shift_plan, staff_members)
        base_fixed_assignments = self.get_base_fixed_assignments(
            shift_plan,
            staff_members,
            month_dates,
        )

        if action == "reset_to_manual":
            with transaction.atomic():
                self.reset_generated_results(shift_plan)
                sync_month_boundary_assignments(shift_plan)
                sync_next_month_boundary_assignments(shift_plan)
            messages.success(request, "自動生成した勤務を削除し、手入力した勤務だけを残しました。")
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        if action == "reset_to_base":
            with transaction.atomic():
                self.reset_all_shift_results(shift_plan)
                sync_month_boundary_assignments(shift_plan)
                sync_next_month_boundary_assignments(shift_plan)
            messages.success(request, "勤務データを削除し、希望休・固定休だけの状態へ戻しました。")
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        submitted_assignments = self.parse_submitted_assignments(
            shift_plan,
            staff_members,
            month_dates,
            base_fixed_assignments,
        )
        validation_errors = self.validate_manual_assignments(
            shift_plan,
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

        if action == "generate":
            try:
                with transaction.atomic():
                    self.save_manual_shift_results(
                        shift_plan,
                        submitted_assignments,
                        existing_results_by_key,
                    )
                    sync_month_boundary_assignments(shift_plan)
                    generation_result = generate_and_save_shift(shift_plan)
                    sync_next_month_boundary_assignments(shift_plan)
            except (ShiftGenerationError, MonthBoundaryConflictError) as error:
                messages.error(request, f"シフトを生成できませんでした。 {error}")
                context = self.build_edit_context(
                    shift_plan,
                    display_assignments=submitted_assignments,
                )
                return render(request, self.template_name, context)

            messages.success(request, "シフトを生成しました。")
            if generation_result.day_staffing_adjustment_message:
                messages.info(
                    request,
                    generation_result.day_staffing_adjustment_message,
                )
            if generation_result.has_violations:
                warning_lines = format_generation_violation_messages(
                    generation_result.violations
                )
                messages.warning(
                    request,
                    " ".join(warning_lines),
                )
            return HttpResponseRedirect(self.get_edit_url(shift_plan))

        with transaction.atomic():
            self.save_manual_shift_results(
                shift_plan,
                submitted_assignments,
                existing_results_by_key,
            )
            sync_next_month_boundary_assignments(shift_plan)
            messages.success(request, "シフトを保存しました。")

        return HttpResponseRedirect(self.get_edit_url(shift_plan))


class ShiftPlanCsvExportView(UserShiftPlanMixin, View):
    """編集画面と同じ表示内容をCSV形式で出力する。"""

    def get(self, request, *args, **kwargs):
        shift_plan = self.get_object()
        context = self.build_edit_context(shift_plan)
        day_headers = context["day_headers"]
        staff_rows = context["staff_rows"]

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="ns_shift_'
            f'{shift_plan.year}_{shift_plan.month:02d}.csv"'
        )

        # Excelで開いた場合も日本語が文字化けしないよう、UTF-8 BOMを先頭に付ける。
        response.write("\ufeff")
        writer = csv.writer(response, lineterminator="\r\n")
        writer.writerow(
            ["スタッフ"]
            + [
                f'{header["date"].day}({header["weekday_label"]})'
                for header in day_headers
            ]
        )

        for row in staff_rows:
            writer.writerow(
                [row["staff_member"].name]
                + [
                    cell["display_label"] if cell["value"] else ""
                    for cell in row["cells"]
                ]
            )

        return response


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
