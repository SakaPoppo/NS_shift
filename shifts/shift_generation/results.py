from __future__ import annotations

from ..models import ShiftResult
from ..services import WORKLIKE_SHIFT_TYPES
from .types import (
    GENERATABLE_SHIFT_TYPES,
    GeneratedShift,
    ShiftGenerationViolation,
    ShiftGenerationViolationType,
    ShiftOptimizationSummary,
)


def format_generation_violation_messages(
    violations: list[ShiftGenerationViolation],
    *,
    limit: int = 5,
) -> list[str]:
    """messages.warning() 用に違反メッセージを短く整形する。"""

    lines = [f"・{violation.message}" for violation in violations[:limit]]
    remaining_count = len(violations) - limit
    if remaining_count > 0:
        lines.append(f"・そのほか {remaining_count} 件")
    return lines


def _solver_value(solver, expression) -> int:
    return int(solver.Value(expression)) if expression is not None else 0


def _build_optimization_summary(
    *,
    solver,
    day_staffing_balance_data,
    night_count_balance_data,
    ability_balance_data,
    long_streak_terms,
    phase_results,
):
    day_count_balance_violation = max(
        _solver_value(solver, day_staffing_balance_data.delta_range) - 1,
        0,
    )
    return ShiftOptimizationSummary(
        total_day_shortage=_solver_value(
            solver, day_staffing_balance_data.total_day_shortage
        ),
        max_day_shortage=_solver_value(
            solver, day_staffing_balance_data.max_day_shortage
        ),
        leader_shortage_total=0,
        qualified_staff_shortage_total=0,
        max_consecutive_violation_count=0,
        night_shift_count_min=(
            _solver_value(solver, night_count_balance_data.night_count_min)
            if night_count_balance_data.night_count_min is not None
            else None
        ),
        night_shift_count_max=(
            _solver_value(solver, night_count_balance_data.night_count_max)
            if night_count_balance_data.night_count_max is not None
            else None
        ),
        night_count_imbalance_violation=_solver_value(
            solver, night_count_balance_data.night_balance_violation
        ),
        total_day_excess=_solver_value(
            solver, day_staffing_balance_data.total_day_excess
        ),
        max_day_count_balance_violation=day_count_balance_violation,
        total_day_count_balance_violation=day_count_balance_violation,
        max_day_ability_total_range=_solver_value(
            solver, ability_balance_data.max_day_range
        ),
        total_day_ability_total_range=_solver_value(
            solver, ability_balance_data.total_day_range
        ),
        max_night_ability_total_range=_solver_value(
            solver, ability_balance_data.max_night_range
        ),
        total_night_ability_total_range=_solver_value(
            solver, ability_balance_data.total_night_range
        ),
        long_streak_penalty=sum(
            _solver_value(solver, term) for term in long_streak_terms
        ),
        phase_statuses={result.name: result.status for result in phase_results},
        phase_optimal_flags={
            result.name: result.optimal for result in phase_results
        },
        night_shift_counts={
            staff_id: _solver_value(solver, count_var)
            for staff_id, count_var in (
                night_count_balance_data.night_count_vars.items()
            )
        },
    )


def _build_generated_shifts(
    *, solver, staff_members, month_dates, shift_vars, fixed_assignments
):
    generated_shifts = []
    for staff_member in staff_members:
        for target_date in month_dates:
            cell_key = (staff_member.id, target_date)
            fixed_shift_type = fixed_assignments.get(cell_key)
            if fixed_shift_type == ShiftResult.ShiftTypeChoices.OFF_REQUEST:
                shift_type = ShiftResult.ShiftTypeChoices.OFF_REQUEST
            elif (
                fixed_shift_type is not None
                and fixed_shift_type not in GENERATABLE_SHIFT_TYPES
            ):
                shift_type = fixed_shift_type
            else:
                shift_type = next(
                    shift_name
                    for shift_name, shift_var in shift_vars[cell_key].items()
                    if solver.Value(shift_var) == 1
                )
            generated_shifts.append(
                GeneratedShift(
                    staff_member_id=staff_member.id,
                    date=target_date,
                    shift_type=shift_type,
                )
            )
    return generated_shifts


def _build_generation_violations(
    *,
    shifts,
    staff_members,
    month_dates,
    effective_rules,
    max_consecutive_work_days,
    night_shift_counts,
):
    shift_map = {
        (generated_shift.staff_member_id, generated_shift.date):
        generated_shift.shift_type
        for generated_shift in shifts
    }
    violations = []
    violations.extend(
        _build_staffing_violations(
            shift_map=shift_map,
            staff_members=staff_members,
            month_dates=month_dates,
            effective_rules=effective_rules,
        )
    )
    violations.extend(
        _build_consecutive_work_violations(
            shift_map=shift_map,
            staff_members=staff_members,
            month_dates=month_dates,
            max_consecutive_work_days=max_consecutive_work_days,
        )
    )
    violations.extend(_build_night_count_imbalance_violation(night_shift_counts))
    return violations


def _build_night_count_imbalance_violation(night_shift_counts):
    if len(night_shift_counts) <= 1:
        return []
    minimum = min(night_shift_counts.values())
    maximum = max(night_shift_counts.values())
    difference = maximum - minimum
    if difference <= 1:
        return []
    return [
        ShiftGenerationViolation(
            violation_type=ShiftGenerationViolationType.NIGHT_COUNT_IMBALANCE,
            message=(
                f"スタッフ間の夜勤回数差が{difference}回あります。"
                "目標は1回以内ですが、固定勤務などの影響により調整できませんでした。"
            ),
            minimum_count=minimum,
            maximum_count=maximum,
            count_difference=difference,
            allowed_difference=1,
            amount=difference - 1,
        )
    ]


def _build_staffing_violations(
    *, shift_map, staff_members, month_dates, effective_rules
):
    violations = []
    for target_date in month_dates:
        actual_day_count = sum(
            1
            for staff_member in staff_members
            if shift_map[(staff_member.id, target_date)]
            == ShiftResult.ShiftTypeChoices.DAY
        )
        rule = effective_rules[target_date]
        if actual_day_count < rule.required_day_staff:
            amount = rule.required_day_staff - actual_day_count
            violations.append(
                ShiftGenerationViolation(
                    violation_type=ShiftGenerationViolationType.DAY_SHORTAGE,
                    message=(
                        f"{target_date.month}月{target_date.day}日の日勤が{amount}人不足しています。"
                        f" 必要人数：{rule.required_day_staff}人 / 実際の人数：{actual_day_count}人"
                    ),
                    date=target_date,
                    required_count=rule.required_day_staff,
                    actual_count=actual_day_count,
                    amount=amount,
                )
            )
    return violations


def _build_consecutive_work_violations(
    *, shift_map, staff_members, month_dates, max_consecutive_work_days
):
    violations = []
    for staff_member in staff_members:
        run_start = None
        run_end = None
        for current_date in month_dates:
            shift_type = shift_map[(staff_member.id, current_date)]
            if shift_type in WORKLIKE_SHIFT_TYPES:
                if run_start is None:
                    run_start = current_date
                run_end = current_date
                continue
            if run_start is not None:
                violations.extend(
                    _build_single_consecutive_violation(
                        staff_member=staff_member,
                        run_start=run_start,
                        run_end=run_end,
                        max_consecutive_work_days=max_consecutive_work_days,
                    )
                )
                run_start = None
                run_end = None
        if run_start is not None:
            violations.extend(
                _build_single_consecutive_violation(
                    staff_member=staff_member,
                    run_start=run_start,
                    run_end=run_end,
                    max_consecutive_work_days=max_consecutive_work_days,
                )
            )
    return violations


def _build_single_consecutive_violation(
    *, staff_member, run_start, run_end, max_consecutive_work_days
):
    consecutive_days = run_end.toordinal() - run_start.toordinal() + 1
    if consecutive_days <= max_consecutive_work_days:
        return []
    return [
        ShiftGenerationViolation(
            violation_type=ShiftGenerationViolationType.MAX_CONSECUTIVE_WORK,
            message=(
                f"{staff_member.name}さんに最大連勤数を超える勤務があります。"
                f" {run_start.month}月{run_start.day}日から{run_end.month}月{run_end.day}日まで"
                f"{consecutive_days}連勤です。 最大連勤数：{max_consecutive_work_days}日"
            ),
            staff_member_id=staff_member.id,
            amount=consecutive_days - max_consecutive_work_days,
            required_count=max_consecutive_work_days,
            actual_count=consecutive_days,
            start_date=run_start,
            end_date=run_end,
        )
    ]
