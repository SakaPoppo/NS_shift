from __future__ import annotations

from ..models import ShiftResult
from .types import (
    GENERATABLE_SHIFT_TYPES,
    GenerationIssue,
    GenerationIssueCode,
    GenerationIssueSeverity,
    GeneratedShift,
    ShiftOptimizationSummary,
)


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


def build_generation_issues(
    *, optimization_summary: ShiftOptimizationSummary
) -> list[GenerationIssue]:
    """求解結果から画面通知用の構造化データを組み立てる。"""

    issues = [
        GenerationIssue(
            code=GenerationIssueCode.SHIFT_GENERATED,
            severity=GenerationIssueSeverity.SUCCESS,
        )
    ]
    above_dates = [
        target_date
        for target_date, delta in optimization_summary.day_staffing_deltas.items()
        if delta > 0
    ]
    if above_dates:
        issues.append(
            GenerationIssue(
                code=GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED,
                severity=GenerationIssueSeverity.INFO,
                dates=above_dates,
                details={
                    "actual_day_counts": optimization_summary.actual_day_counts,
                    "required_day_counts": optimization_summary.required_day_counts,
                },
            )
        )
    below_dates = [
        target_date
        for target_date, delta in optimization_summary.day_staffing_deltas.items()
        if delta < 0
    ]
    if below_dates:
        issues.append(
            GenerationIssue(
                code=GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                severity=GenerationIssueSeverity.WARNING,
                dates=below_dates,
            )
        )
    imbalance_dates = [
        target_date
        for target_date, delta in optimization_summary.day_staffing_deltas.items()
        if abs(delta) >= 2
    ]
    if imbalance_dates:
        issues.append(
            GenerationIssue(
                code=GenerationIssueCode.DAY_STAFFING_IMBALANCE,
                severity=GenerationIssueSeverity.WARNING,
                dates=imbalance_dates,
                details={
                    "required_day_counts": optimization_summary.required_day_counts,
                    "actual_day_counts": optimization_summary.actual_day_counts,
                    "minimum_delta": optimization_summary.minimum_day_staffing_delta,
                    "maximum_delta": optimization_summary.maximum_day_staffing_delta,
                },
            )
        )
    night_counts = optimization_summary.night_shift_counts
    if len(night_counts) > 1:
        difference = max(night_counts.values()) - min(night_counts.values())
        if difference > 1:
            issues.append(
                GenerationIssue(
                    code=GenerationIssueCode.NIGHT_COUNT_IMBALANCE,
                    severity=GenerationIssueSeverity.WARNING,
                    staff_ids=list(night_counts),
                    details={
                        "count_difference": difference,
                        "minimum_count": min(night_counts.values()),
                        "maximum_count": max(night_counts.values()),
                    },
                )
            )
    incomplete_items = [
        name
        for name, status in optimization_summary.phase_statuses.items()
        if status in {"UNKNOWN", "NOT_RUN"}
    ]
    if incomplete_items:
        issues.append(
            GenerationIssue(
                code=GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                severity=GenerationIssueSeverity.WARNING,
                details={"incomplete_items": incomplete_items},
            )
        )
    return issues
