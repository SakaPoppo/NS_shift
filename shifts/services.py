import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from django.db import transaction
from jpholiday import JPHoliday

from staff.models import StaffMember

from .models import DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult

"""画面表示以外の共通業務ロジック用ファイル"""

_jp_holiday = JPHoliday()


WORKLIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.TRAINING,
}
OFF_LIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.OFF,
    ShiftResult.ShiftTypeChoices.OFF_REQUEST,
    ShiftResult.ShiftTypeChoices.PAID_LEAVE,
    ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
}


class MonthBoundaryConflictError(ValueError):
    """月跨ぎ勤務が既存の基礎データ・勤務と競合した。"""


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

# 1.日付条件決定用の関数

def get_effective_rule_for_date(shift_plan: ShiftPlan, target_date):
    """
    指定日の優先度決定用
    優先順位は「特定日条件 > 曜日条件 > 月共通条件」
    上書き用フィールドが None の場合は、その条件では値を確定せず下位条件へフォールバックする
    """
    shift_rule = shift_plan.shift_rule
    weekday_rule_map = getattr(shift_plan, "_weekday_rule_map", None)
    date_rule_map = getattr(shift_plan, "_date_rule_map", None)

    if weekday_rule_map is not None:
        weekday_rule = weekday_rule_map.get(target_date.weekday())
    else:
        weekday_rule = shift_plan.weekday_rules.filter(day_of_week=target_date.weekday()).first()

    if date_rule_map is not None:
        date_rule = date_rule_map.get(target_date)
    else:
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
    """指定月の最初の日と最後の日を文字列で返す
    例：("2026-07-01", "2026-07-31")"""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day:02d}"


def get_month_dates(year, month):
    """指定月の全日付をリストで返す"""
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last_day + 1)]


@lru_cache(maxsize=120)
def get_japanese_holiday_dates(year: int, month: int) -> frozenset[date]:
    """対象月の日本の祝日を取得"""
    return frozenset(holiday.date for holiday in _jp_holiday.month_holidays(year, month))


def get_previous_month_year_and_month(year: int, month: int) -> tuple[int, int]:
    # 前月が何年何月か返す
    return (year - 1, 12) if month == 1 else (year, month - 1)

# 2.前月結果の自動引き継ぎ用の関数
def get_usable_previous_shift_plan(shift_plan: ShiftPlan) -> ShiftPlan | None:
    year, month = get_previous_month_year_and_month(shift_plan.year, shift_plan.month)
    return ShiftPlan.objects.filter(
        user=shift_plan.user,
        year=year,
        month=month,
        status__in=[ShiftPlan.StatusChoices.GENERATED, ShiftPlan.StatusChoices.CONFIRMED],
    ).first()


def calculate_previous_consecutive_work_days(
    previous_shift_plan: ShiftPlan, staff_member: StaffMember
) -> int:
    """前月末から、欠損または非勤務日に当たるまで勤務日を逆算する。"""
    last_date = get_month_dates(previous_shift_plan.year, previous_shift_plan.month)[-1]
    results = {
        result.date: result.shift_type
        for result in ShiftResult.objects.filter(
            shift_plan=previous_shift_plan,
            staff_member=staff_member,
            date__lte=last_date,
        )
    }
    count = 0
    current_date = last_date
    while results.get(current_date) in WORKLIKE_SHIFT_TYPES:
        count += 1
        current_date -= timedelta(days=1)
    return count


def build_shift_carryovers(shift_plan: ShiftPlan) -> list[ShiftCarryover]:
    """利用可能な前月結果がある在籍スタッフだけを自動引き継ぎする。"""
    previous_plan = get_usable_previous_shift_plan(shift_plan)
    if previous_plan is None:
        return []
    last_date = get_month_dates(previous_plan.year, previous_plan.month)[-1]
    staff_members = list(StaffMember.objects.filter(user=shift_plan.user, is_active=True))
    last_results = {
        result.staff_member_id: result
        for result in ShiftResult.objects.filter(
            shift_plan=previous_plan,
            staff_member__in=staff_members,
            date=last_date,
        )
    }
    carryovers = []
    for staff_member in staff_members:
        last_result = last_results.get(staff_member.id)
        if last_result is None:
            continue
        consecutive = (
            calculate_previous_consecutive_work_days(previous_plan, staff_member)
            if last_result.shift_type in WORKLIKE_SHIFT_TYPES else 0
        )
        carryover, _ = ShiftCarryover.objects.update_or_create(
            shift_plan=shift_plan,
            staff_member=staff_member,
            defaults={
                "source": ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
                "previous_shift_plan": previous_plan,
                "previous_last_shift_type": last_result.shift_type,
                "previous_consecutive_work_days": consecutive,
            },
        )
        carryovers.append(carryover)
    return carryovers


def save_manual_shift_carryovers(shift_plan: ShiftPlan, values: dict[int, tuple[str, int]]) -> None:
    for staff_member in StaffMember.objects.filter(
        user=shift_plan.user, is_active=True, id__in=values
    ):
        shift_type, consecutive = values[staff_member.id]
        if shift_type in OFF_LIKE_SHIFT_TYPES or not shift_type:
            consecutive = 0
        ShiftCarryover.objects.update_or_create(
            shift_plan=shift_plan,
            staff_member=staff_member,
            defaults={
                "source": ShiftCarryover.SourceChoices.MANUAL,
                "previous_shift_plan": None,
                "previous_last_shift_type": shift_type or None,
                "previous_consecutive_work_days": consecutive,
            },
        )


def _boundary_assignments(shift_plan: ShiftPlan, carryover: ShiftCarryover) -> dict[date, str]:
    first = date(shift_plan.year, shift_plan.month, 1)
    previous = carryover.previous_last_shift_type
    if previous == ShiftResult.ShiftTypeChoices.NIGHT:
        assignments = {first: ShiftResult.ShiftTypeChoices.AFTER_NIGHT}
        rule = getattr(shift_plan, "shift_rule", None)
        if rule and calendar.monthrange(shift_plan.year, shift_plan.month)[1] >= 2:
            assignments[first + timedelta(days=1)] = (
                ShiftResult.ShiftTypeChoices.OFF
                if rule.night_shift_next_day_off
                else ShiftResult.ShiftTypeChoices.NIGHT
            )
        return assignments
    if previous == ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
        return {first: ShiftResult.ShiftTypeChoices.OFF}
    return {}


@transaction.atomic
def sync_month_boundary_assignments(shift_plan: ShiftPlan) -> list[ShiftResult]:
    """必要な月初勤務を冪等同期する。競合時は既存境界勤務も保持する。"""
    carryovers = list(
        shift_plan.carryovers.select_related("staff_member").filter(
            staff_member__user=shift_plan.user,
            staff_member__is_active=True,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
        )
    )
    wanted = {
        (carryover.staff_member_id, target_date): (carryover, shift_type)
        for carryover in carryovers
        for target_date, shift_type in _boundary_assignments(shift_plan, carryover).items()
    }
    existing = {
        (result.staff_member_id, result.date): result
        for result in ShiftResult.objects.select_for_update().filter(
            shift_plan=shift_plan, date__day__lte=2
        )
    }
    requests = set(
        DayOffRequest.objects.filter(shift_plan=shift_plan, date__day__lte=2)
        .values_list("staff_member_id", "date")
    )
    regular = {
        staff.id: {day_off.day_of_week for day_off in staff.regular_days_off.all()}
        for staff in StaffMember.objects.filter(id__in=[c.staff_member_id for c in carryovers])
        .prefetch_related("regular_days_off")
    }
    errors = []
    for key, (carryover, required_type) in wanted.items():
        staff_id, target_date = key
        current = existing.get(key)
        regular_off = target_date.weekday() in regular.get(staff_id, set())
        base_off = key in requests or regular_off
        # 2日目のOFFは既に保証された休みと重複保存しない。一方、明けや夜勤とは競合する。
        if base_off and required_type == ShiftResult.ShiftTypeChoices.OFF:
            continue
        # 月境界の2日目だけは曜日固定休を優先し、明け翌日夜勤の例外を許可する。
        if target_date.day == 2 and regular_off:
            continue
        if base_off or (current and current.lock_reason != ShiftResult.LockReasonChoices.MONTH_BOUNDARY):
            reason = "希望休または曜日固定休" if base_off else current.get_shift_type_display()
            errors.append(
                f"{carryover.staff_member.name}さんは前月末勤務のため、"
                f"{target_date.month}月{target_date.day}日は{dict(ShiftResult.ShiftTypeChoices.choices)[required_type]}"
                f"にする必要がありますが、{reason}が登録されています。"
            )
    if errors:
        raise MonthBoundaryConflictError(" ".join(errors))

    boundary_qs = ShiftResult.objects.filter(
        shift_plan=shift_plan,
        lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY,
    )
    obsolete_ids = [result.id for result in boundary_qs if (result.staff_member_id, result.date) not in wanted]
    if obsolete_ids:
        ShiftResult.objects.filter(id__in=obsolete_ids).delete()
    saved = []
    for key, (_, shift_type) in wanted.items():
        staff_id, target_date = key
        regular_off = target_date.weekday() in regular.get(staff_id, set())
        if (
            (shift_type == ShiftResult.ShiftTypeChoices.OFF and (key in requests or regular_off))
            or (target_date.day == 2 and regular_off)
        ):
            ShiftResult.objects.filter(
                shift_plan=shift_plan, staff_member_id=staff_id, date=target_date,
                lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY,
            ).delete()
            continue
        result, _ = ShiftResult.objects.update_or_create(
            shift_plan=shift_plan, staff_member_id=staff_id, date=target_date,
            defaults={
                "shift_type": shift_type,
                "input_type": ShiftResult.InputTypeChoices.GENERATED,
                "is_locked": True,
                "lock_reason": ShiftResult.LockReasonChoices.MONTH_BOUNDARY,
            },
        )
        saved.append(result)
    return saved


def sync_next_month_boundary_assignments(shift_plan: ShiftPlan) -> None:
    next_year, next_month = (shift_plan.year + 1, 1) if shift_plan.month == 12 else (shift_plan.year, shift_plan.month + 1)
    next_plan = ShiftPlan.objects.filter(user=shift_plan.user, year=next_year, month=next_month).first()
    if next_plan:
        build_shift_carryovers(next_plan)
        sync_month_boundary_assignments(next_plan)
