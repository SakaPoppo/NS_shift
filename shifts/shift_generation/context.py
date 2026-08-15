from __future__ import annotations

from django.db.models import Q

from staff.models import StaffMember

from ..models import DayOffRequest, ShiftResult
from ..services import (
    OFF_LIKE_SHIFT_TYPES,
    get_effective_rule_for_date,
    get_japanese_holiday_dates,
    get_month_dates,
)
from .types import GenerationContext, ShiftGenerationError


def load_generation_context(shift_plan) -> GenerationContext:
    """生成データを読み込み、固定条件だけで判定できる矛盾を検出する。"""

    shift_rule = getattr(shift_plan, "shift_rule", None)
    if shift_rule is None:
        raise ShiftGenerationError("シフト条件が未設定のため、自動生成を開始できません。")

    month_dates = get_month_dates(shift_plan.year, shift_plan.month)
    holiday_dates = get_japanese_holiday_dates(shift_plan.year, shift_plan.month)
    staff_members = list(
        StaffMember.objects.filter(
            user=shift_plan.user,
            is_active=True,
        ).prefetch_related("regular_days_off")
    )
    if not staff_members:
        raise ShiftGenerationError("有効なスタッフがいないため、自動生成できません。")

    weekday_rules = list(shift_plan.weekday_rules.all())
    date_rules = list(shift_plan.date_rules.all())
    shift_plan._weekday_rule_map = {
        rule.day_of_week: rule for rule in weekday_rules
    }
    shift_plan._date_rule_map = {rule.target_date: rule for rule in date_rules}

    day_off_requests = {
        (request.staff_member_id, request.date): request
        for request in DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
    }
    fixed_results = {
        (result.staff_member_id, result.date): result
        for result in ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).filter(
            Q(input_type=ShiftResult.InputTypeChoices.MANUAL) | Q(is_locked=True)
        )
    }
    regular_day_offs = {
        staff.id: {
            day_off.day_of_week for day_off in staff.regular_days_off.all()
        }
        for staff in staff_members
    }
    fixed_assignments = _build_fixed_assignments(
        month_dates=month_dates,
        staff_members=staff_members,
        day_off_requests=day_off_requests,
        fixed_results=fixed_results,
        regular_day_offs=regular_day_offs,
        holiday_dates=holiday_dates,
    )
    effective_rules = {
        target_date: get_effective_rule_for_date(shift_plan, target_date)
        for target_date in month_dates
    }
    night_capable_count = sum(staff.can_night_shift for staff in staff_members)
    for target_date, effective_rule in effective_rules.items():
        if effective_rule.required_night_staff > night_capable_count:
            raise ShiftGenerationError(
                f"{target_date.month}月{target_date.day}日は夜勤が"
                f"{effective_rule.required_night_staff}人必要ですが、夜勤を配置できるスタッフが"
                f"{night_capable_count}人しかいません。"
            )

    previous_consecutive_work_days = {
        carryover.staff_member_id: carryover.previous_consecutive_work_days
        for carryover in shift_plan.carryovers.filter(staff_member__in=staff_members)
    }
    mandatory_off_counts = {
        staff.id: len(
            {
                target_date
                for target_date in month_dates
                if target_date.weekday()
                in regular_day_offs.get(staff.id, set())
                or (staff.is_holiday_off and target_date in holiday_dates)
                or (
                    fixed_results.get((staff.id, target_date)) is not None
                    and fixed_results[(staff.id, target_date)].lock_reason
                    == ShiftResult.LockReasonChoices.MONTH_BOUNDARY
                    and fixed_results[(staff.id, target_date)].shift_type
                    == ShiftResult.ShiftTypeChoices.OFF
                )
            }
        )
        for staff in staff_members
    }
    effective_off_days = {
        staff_id: max(shift_rule.off_days_per_staff, count)
        for staff_id, count in mandatory_off_counts.items()
    }
    for staff in staff_members:
        fixed_off_count = sum(
            fixed_assignments.get((staff.id, target_date)) in OFF_LIKE_SHIFT_TYPES
            for target_date in month_dates
        )
        if fixed_off_count > effective_off_days[staff.id]:
            raise ShiftGenerationError(
                f"{staff.name} は月休日数 {effective_off_days[staff.id]} 日を超えています。"
                "月休日数または希望休・有給などを見直してください。"
            )

    _validate_fixed_assignments(
        staff_members=staff_members,
        month_dates=month_dates,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
    )
    return GenerationContext(
        shift_rule=shift_rule,
        month_dates=month_dates,
        staff_members=staff_members,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
        previous_consecutive_work_days=previous_consecutive_work_days,
        effective_off_days=effective_off_days,
    )


def _build_fixed_assignments(
    *,
    month_dates,
    staff_members,
    day_off_requests,
    fixed_results,
    regular_day_offs,
    holiday_dates,
):
    fixed_assignments = {}
    for staff in staff_members:
        regular_days = regular_day_offs.get(staff.id, set())
        for target_date in month_dates:
            cell_key = (staff.id, target_date)
            if cell_key in day_off_requests:
                fixed_assignments[cell_key] = (
                    ShiftResult.ShiftTypeChoices.OFF_REQUEST
                )
            elif (
                target_date.weekday() in regular_days
                or (staff.is_holiday_off and target_date in holiday_dates)
            ):
                fixed_assignments[cell_key] = ShiftResult.ShiftTypeChoices.OFF

    for cell_key, shift_result in fixed_results.items():
        existing_shift_type = fixed_assignments.get(cell_key)
        if (
            existing_shift_type is not None
            and existing_shift_type != shift_result.shift_type
        ):
            staff_member_id, target_date = cell_key
            raise ShiftGenerationError(
                f"{target_date:%Y-%m-%d} のスタッフID {staff_member_id} は、"
                f"固定条件「{existing_shift_type}」と保存済み勤務「{shift_result.shift_type}」が競合しています。"
            )
        fixed_assignments[cell_key] = shift_result.shift_type
    return fixed_assignments


def _validate_fixed_assignments(
    *, staff_members, month_dates, fixed_assignments, effective_rules
):
    staff_by_id = {staff.id: staff for staff in staff_members}
    for staff in staff_members:
        for index, target_date in enumerate(month_dates):
            fixed_shift_type = fixed_assignments.get((staff.id, target_date))
            if fixed_shift_type is None:
                continue
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.NIGHT
                and not staff.can_night_shift
            ):
                raise ShiftGenerationError(
                    f"{staff.name} は夜勤不可ですが、{target_date:%Y-%m-%d} に夜勤が固定されています。"
                )
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                and index > 0
            ):
                previous_shift_type = fixed_assignments.get(
                    (staff.id, month_dates[index - 1])
                )
                if (
                    previous_shift_type is not None
                    and previous_shift_type != ShiftResult.ShiftTypeChoices.NIGHT
                ):
                    raise ShiftGenerationError(
                        f"{staff.name} の {target_date:%Y-%m-%d} の明けは、前日の固定勤務と整合しません。"
                    )
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                and index == 0
                and len(month_dates) >= 2
                and not effective_rules[target_date].night_shift_next_day_off
            ):
                next_shift_type = fixed_assignments.get(
                    (staff.id, month_dates[index + 1])
                )
                if next_shift_type is not None and next_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {ShiftResult.ShiftTypeChoices.NIGHT}
                ):
                    raise ShiftGenerationError(
                        f"{staff.name} の {target_date:%Y-%m-%d} の明けは、翌日の固定勤務と整合しません。"
                    )
            if fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                continue
            if index + 1 >= len(month_dates):
                continue
            next_shift_type = fixed_assignments.get(
                (staff.id, month_dates[index + 1])
            )
            if (
                next_shift_type is not None
                and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            ):
                raise ShiftGenerationError(
                    f"{staff.name} の {target_date:%Y-%m-%d} の夜勤は、翌日の固定勤務と整合しません。"
                )
            if index + 2 >= len(month_dates):
                continue
            third_shift_type = fixed_assignments.get(
                (staff.id, month_dates[index + 2])
            )
            rule = effective_rules[target_date]
            if rule.night_shift_next_day_off:
                if (
                    third_shift_type is not None
                    and third_shift_type not in OFF_LIKE_SHIFT_TYPES
                ):
                    raise ShiftGenerationError(
                        f"{staff.name} の {target_date:%Y-%m-%d} の夜勤は、2日後の固定勤務と整合しません。"
                    )
            elif third_shift_type is not None and third_shift_type not in (
                OFF_LIKE_SHIFT_TYPES
                | {ShiftResult.ShiftTypeChoices.NIGHT}
            ):
                raise ShiftGenerationError(
                    f"{staff.name} の {target_date:%Y-%m-%d} の夜勤は、2日後の固定勤務と整合しません。"
                )

    unknown_staff_ids = {
        staff_member_id for staff_member_id, _ in fixed_assignments
    } - set(staff_by_id)
    if unknown_staff_ids:
        raise ShiftGenerationError(
            "固定勤務に対象外スタッフが含まれているため、自動生成できません。"
        )
