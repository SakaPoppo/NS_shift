"""お試し版の固定データから生成用コンテキストを構築する。"""

import calendar
from dataclasses import replace
from datetime import date

from shifts.models import ShiftResult
from shifts.services import EffectiveShiftRule, get_month_dates
from shifts.shift_generation.types import GenerationContext

from .data import (
    TRIAL_PREVIOUS_AFTER_NIGHT_STAFF_IDS,
    TRIAL_PREVIOUS_NIGHT_STAFF_IDS,
    TRIAL_STAFF_MEMBERS,
    TrialShiftRule,
)


def get_trial_target_year_month(reference_date: date) -> tuple[int, int]:
    """基準日の翌月をお試し版の対象年月として返す。"""

    if reference_date.month == 12:
        return reference_date.year + 1, 1
    return reference_date.year, reference_date.month + 1


def get_trial_off_days(year: int, month: int) -> int:
    """対象月の日数に対応するお試し版の月休日数を返す。"""

    return 9 if calendar.monthrange(year, month)[1] == 28 else 10


def build_trial_generation_context(
    reference_date: date,
    manual_assignments: dict[tuple[int, date], str] | None = None,
) -> GenerationContext:
    """固定サンプルデータだけでGenerationContextを構築する。"""

    year, month = get_trial_target_year_month(reference_date)
    month_dates = get_month_dates(year, month)
    shift_rule = TrialShiftRule(off_days_per_staff=get_trial_off_days(year, month))
    effective_rule = EffectiveShiftRule(
        required_day_staff=shift_rule.required_day_staff,
        required_day_staff_override=None,
        required_night_staff=shift_rule.required_night_staff,
        required_leader_staff=shift_rule.required_leader_staff,
        min_ability_level=None,
        min_ability_level_staff_count=None,
        max_consecutive_work_days=shift_rule.max_consecutive_work_days,
        night_shift_next_day_off=shift_rule.night_shift_next_day_off,
    )
    fixed_assignments = _build_trial_fixed_assignments(month_dates)

    context = GenerationContext(
        shift_rule=shift_rule,
        month_dates=month_dates,
        staff_members=list(TRIAL_STAFF_MEMBERS),
        fixed_assignments=fixed_assignments,
        effective_rules={target_date: effective_rule for target_date in month_dates},
        previous_consecutive_work_days={
            staff.id: 0 for staff in TRIAL_STAFF_MEMBERS
        },
        effective_off_days={
            staff.id: shift_rule.off_days_per_staff for staff in TRIAL_STAFF_MEMBERS
        },
    )
    if not manual_assignments:
        return context

    fixed_assignments = dict(context.fixed_assignments)
    fixed_assignments.update(manual_assignments)
    return replace(
        context,
        fixed_assignments=fixed_assignments,
        user_override_assignment_keys=set(manual_assignments),
    )


def _build_trial_fixed_assignments(month_dates: list[date]) -> dict[tuple[int, date], str]:
    first_date, second_date = month_dates[:2]
    fixed_assignments = {
        (staff.id, target_date): ShiftResult.ShiftTypeChoices.OFF
        for staff in TRIAL_STAFF_MEMBERS
        for target_date in month_dates
        if target_date.weekday() in staff.regular_days_off
    }
    for staff_id in TRIAL_PREVIOUS_NIGHT_STAFF_IDS:
        fixed_assignments[(staff_id, first_date)] = (
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT
        )
        fixed_assignments[(staff_id, second_date)] = ShiftResult.ShiftTypeChoices.OFF
    for staff_id in TRIAL_PREVIOUS_AFTER_NIGHT_STAFF_IDS:
        fixed_assignments[(staff_id, first_date)] = ShiftResult.ShiftTypeChoices.OFF
    return fixed_assignments
