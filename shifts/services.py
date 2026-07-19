import calendar
from dataclasses import dataclass
from datetime import date

from .models import ShiftPlan


@dataclass(frozen=True)
class EffectiveShiftRule:
    """ある1日に対して解決済みの条件セット。

    月共通条件・曜日条件・特定日条件を解決した後の値をまとめる。
    生成処理側から誤って書き換えないよう、読み取り専用で扱う。
    """

    required_day_staff: int
    required_night_staff: int
    required_leader_staff: int
    min_ability_level: int | None
    min_ability_level_staff_count: int | None
    off_days_per_staff: int
    max_consecutive_work_days: int
    night_shift_next_day_off: bool


def get_effective_rule_for_date(shift_plan: ShiftPlan, target_date):
    """指定日の最終条件を返す。

    優先順位は「特定日条件 > 曜日条件 > 月共通条件」。
    上書き用フィールドが None の場合は、その条件では値を確定せず下位条件へフォールバックする。
    """
    shift_rule = shift_plan.shift_rule
    weekday_rule = shift_plan.weekday_rules.filter(day_of_week=target_date.weekday()).first()
    date_rule = shift_plan.date_rules.filter(target_date=target_date).first()

    def resolve(field_name, default_value):
        # None は「0」ではなく「この条件では上書きしない」を意味する。
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
    """その月に入力可能な日付範囲を HTML date input 用の文字列で返す。"""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


def get_month_dates(year, month):
    """指定した年月に含まれる日付一覧を date オブジェクトで返す。"""
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last_day + 1)]
