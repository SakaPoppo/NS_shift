"""生成コンテキストを外部の最適化器へ渡す形式へ変換する。"""

from __future__ import annotations

from .types import GenerationContext


def build_optimizer_payload(context: GenerationContext) -> dict:
    """JSONシリアライズ可能な最適化器向けpayloadを組み立てる。

    ``effective_rules`` と ``effective_off_days`` はDjango側で解決済みの
    最終条件を渡すため、元の ``shift_rule`` は重複して含めない。
    """

    return {
        "month_dates": [
            target_date.isoformat() for target_date in context.month_dates
        ],
        "staff_members": [
            {
                "id": staff_member.id,
                "role": str(staff_member.role),
                "ability_level": staff_member.ability_level,
                "can_night_shift": staff_member.can_night_shift,
                "regular_days_off": sorted(
                    day_off.day_of_week
                    for day_off in staff_member.regular_days_off.all()
                ),
            }
            for staff_member in context.staff_members
        ],
        "fixed_assignments": [
            {
                "staff_id": staff_id,
                "date": target_date.isoformat(),
                "shift_type": str(shift_type),
            }
            for (staff_id, target_date), shift_type in sorted(
                context.fixed_assignments.items(),
                key=lambda assignment: (
                    assignment[0][0],
                    assignment[0][1],
                ),
            )
        ],
        "effective_rules": [
            {
                "date": target_date.isoformat(),
                "required_day_staff": effective_rule.required_day_staff,
                "required_night_staff": effective_rule.required_night_staff,
                "required_leader_staff": effective_rule.required_leader_staff,
                "min_ability_level": effective_rule.min_ability_level,
                "min_ability_level_staff_count": (
                    effective_rule.min_ability_level_staff_count
                ),
                "max_consecutive_work_days": (
                    effective_rule.max_consecutive_work_days
                ),
                "night_shift_next_day_off": (
                    effective_rule.night_shift_next_day_off
                ),
            }
            for target_date, effective_rule in sorted(
                context.effective_rules.items(),
                key=lambda rule: rule[0],
            )
        ],
        "previous_consecutive_work_days": [
            {
                "staff_id": staff_id,
                "previous_consecutive_work_days": consecutive_work_days,
            }
            for staff_id, consecutive_work_days in sorted(
                context.previous_consecutive_work_days.items()
            )
        ],
        "effective_off_days": [
            {"staff_id": staff_id, "off_days": off_days}
            for staff_id, off_days in sorted(context.effective_off_days.items())
        ],
    }
