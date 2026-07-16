import calendar
from dataclasses import dataclass

from .models import ShiftPlan


@dataclass(frozen=True)
class EffectiveShiftRule:
    required_day_staff: int
    required_night_staff: int
    required_leader_staff: int
    min_ability_level: int | None
    min_ability_level_staff_count: int | None
    off_days_per_staff: int
    max_consecutive_work_days: int
    night_shift_next_day_off: bool


def get_effective_rule_for_date(shift_plan: ShiftPlan, target_date):
    shift_rule = shift_plan.shift_rule
    weekday_rule = shift_plan.weekday_rules.filter(day_of_week=target_date.weekday()).first()
    date_rule = shift_plan.date_rules.filter(target_date=target_date).first()

    def resolve(field_name, default_value):
        if date_rule and getattr(date_rule, field_name) is not None:
            return getattr(date_rule, field_name)
        if weekday_rule and getattr(weekday_rule, field_name) is not None:
            return getattr(weekday_rule, field_name)
        return default_value

    return EffectiveShiftRule(
        required_day_staff=resolve("required_day_staff", shift_rule.required_day_staff),
        required_night_staff=resolve("required_night_staff", shift_rule.required_night_staff),
        required_leader_staff=resolve("required_leader_staff", shift_rule.required_leader_staff),
        min_ability_level=resolve("min_ability_level", None),
        min_ability_level_staff_count=resolve("min_ability_level_staff_count", None),
        off_days_per_staff=shift_rule.off_days_per_staff,
        max_consecutive_work_days=shift_rule.max_consecutive_work_days,
        night_shift_next_day_off=shift_rule.night_shift_next_day_off,
    )


def get_month_date_range(year, month):
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"
