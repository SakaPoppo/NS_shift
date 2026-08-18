from __future__ import annotations

from collections.abc import Iterable

from ..models import ShiftResult
from .types import (
    GENERATABLE_SHIFT_TYPES,
    GeneratedShift,
    ShiftGenerationViolation,
    ShiftGenerationViolationType,
    ShiftOptimizationSummary,
)


DAY_STAFFING_ADJUSTMENT_MESSAGE_PREFIX = (
    "設定した必要日勤数ではシフト最適化ができなかったため、"
)
OPTIMIZATION_INCOMPLETE_MESSAGES = {
    "day_ability_balance": (
        "処理時間の上限に達したため、"
        "日勤能力配置・連勤配置の調整を完了できませんでした。"
        "夜勤回数・日勤人数まで調整したシフトを使用しています。"
    ),
    "long_streak": (
        "処理時間の上限に達したため、"
        "連勤配置の調整を完了できませんでした。"
        "夜勤回数・日勤人数・能力配置まで調整したシフトを使用しています。"
    ),
    "night_ability_balance": (
    "処理時間の上限に達したため、"
    "夜勤能力配置の調整を完了できませんでした。"
    "夜勤回数を調整したシフトを使用しています。"
),
}


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


def build_day_staffing_adjustment_message(
    *,
    optimization_summary: ShiftOptimizationSummary,
    required_day_counts: Iterable[int],
) -> str | None:
    """必要日勤人数から調整した結果を、月全体で1件の通知にまとめる。"""

    minimum_delta = optimization_summary.minimum_day_staffing_delta
    maximum_delta = optimization_summary.maximum_day_staffing_delta
    if minimum_delta == 0 and maximum_delta == 0:
        return None

    if len(set(required_day_counts)) == 1:
        actual_count_range = _format_count_range(
            optimization_summary.minimum_actual_day_count,
            optimization_summary.maximum_actual_day_count,
        )
        return (
            f"{DAY_STAFFING_ADJUSTMENT_MESSAGE_PREFIX}"
            f"日勤数：{actual_count_range}人で最適化を行なっています。"
        )

    delta_range = _format_day_staffing_delta_range(
        minimum_delta,
        maximum_delta,
    )
    range_suffix = "" if minimum_delta == maximum_delta else "の範囲"
    return (
        f"{DAY_STAFFING_ADJUSTMENT_MESSAGE_PREFIX}"
        f"各日の設定人数に対して{delta_range}人{range_suffix}で"
        "最適化を行なっています。"
    )


def build_optimization_incomplete_message(
    *, optimization_summary: ShiftOptimizationSummary
) -> str | None:
    """後半フェーズのUNKNOWNで採用した途中解について通知する。"""

    for phase_name, message in OPTIMIZATION_INCOMPLETE_MESSAGES.items():
        if optimization_summary.phase_statuses.get(phase_name) == "UNKNOWN":
            return message
    return None


def _format_count_range(minimum: int, maximum: int) -> str:
    if minimum == maximum:
        return str(minimum)
    return f"{minimum}〜{maximum}"


def _format_day_staffing_delta_range(minimum: int, maximum: int) -> str:
    """差分の向きが自然に読めるよう、全角符号付きで整形する。"""

    if minimum == maximum:
        return _format_signed_count(minimum)
    if minimum > 0:
        return f"＋{minimum}〜{maximum}"
    if maximum < 0:
        return f"－{abs(maximum)}〜{abs(minimum)}"
    if minimum == 0:
        return f"0〜＋{maximum}"
    if maximum == 0:
        return f"－{abs(minimum)}〜0"
    return f"－{abs(minimum)}〜＋{maximum}"


def _format_signed_count(value: int) -> str:
    if value > 0:
        return f"＋{value}"
    if value < 0:
        return f"－{abs(value)}"
    return "0"


def _solver_value(solver, expression) -> int:
    return int(solver.Value(expression)) if expression is not None else 0


def _build_optimization_summary(
    *,
    solver,
    day_staffing_balance_data,
    night_count_balance_data,
    long_streak_terms,
    phase_results,
):
    minimum_delta = _solver_value(
        solver, day_staffing_balance_data.minimum_delta
    )
    maximum_delta = _solver_value(
        solver, day_staffing_balance_data.maximum_delta
    )
    delta_range = _solver_value(solver, day_staffing_balance_data.delta_range)
    actual_day_counts = {
        target_date: _solver_value(solver, actual_count_var)
        for target_date, actual_count_var in (
            day_staffing_balance_data.actual_day_count_vars.items()
        )
    }
    required_day_counts = dict(
        day_staffing_balance_data.required_day_counts
    )
    day_staffing_deltas = {
        target_date: _solver_value(solver, delta_var)
        for target_date, delta_var in (
            day_staffing_balance_data.day_staffing_delta_vars.items()
        )
    }
    return ShiftOptimizationSummary(
        total_actual_day_count=_solver_value(
            solver, day_staffing_balance_data.total_actual_day_count
        ),
        total_required_day_count=(
            day_staffing_balance_data.total_required_day_count
        ),
        minimum_day_staffing_delta=minimum_delta,
        maximum_day_staffing_delta=maximum_delta,
        day_staffing_delta_range=delta_range,
        minimum_actual_day_count=min(actual_day_counts.values(), default=0),
        maximum_actual_day_count=max(actual_day_counts.values(), default=0),
        actual_day_counts=actual_day_counts,
        required_day_counts=required_day_counts,
        day_staffing_deltas=day_staffing_deltas,
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
    optimization_summary: ShiftOptimizationSummary,
) -> list[ShiftGenerationViolation]:
    violations = []
    violations.extend(
        _build_day_staffing_imbalance_violation(optimization_summary)
    )
    violations.extend(
        _build_night_count_imbalance_violation(
            optimization_summary.night_shift_counts
        )
    )
    return violations


def _build_day_staffing_imbalance_violation(
    optimization_summary: ShiftOptimizationSummary,
) -> list[ShiftGenerationViolation]:
    difference = optimization_summary.day_staffing_delta_range
    if difference <= 1:
        return []
    return [
        ShiftGenerationViolation(
            violation_type=(
                ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE
            ),
            message=(
                "固定勤務や勤務条件の影響により、日勤人数を均等に配置できませんでした。"
                "可能な範囲で均等化しています。"
            ),
            minimum_count=(
                optimization_summary.minimum_day_staffing_delta
            ),
            maximum_count=(
                optimization_summary.maximum_day_staffing_delta
            ),
            count_difference=difference,
            allowed_difference=1,
            amount=difference - 1,
        )
    ]


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
