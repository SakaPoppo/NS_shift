import calendar
from dataclasses import dataclass

from .models import ShiftPlan


@dataclass(frozen=True)
class EffectiveShiftRule:
    """ある1日について最終的に適用される条件セット。

    受け取るもの:
    - 日勤数、夜勤数、リーダー数、勤務レベル条件などの確定値

    返すもの:
    - view や生成処理から参照しやすい読み取り専用データ
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
    # ShiftPlan と target_date を受け取り、共通条件・曜日条件・特定日条件を解決した結果を返す。
    shift_rule = shift_plan.shift_rule
    weekday_rule = shift_plan.weekday_rules.filter(day_of_week=target_date.weekday()).first()
    date_rule = shift_plan.date_rules.filter(target_date=target_date).first()

    def resolve(field_name, default_value):
        # 特定日条件 > 曜日条件 > 月共通条件 の優先順で値を1つ返す。
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
    # year と month を受け取り、その月の開始日と終了日を YYYY-MM-DD 文字列で返す。
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"
