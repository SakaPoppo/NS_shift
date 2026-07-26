"""シフト自動生成の公開窓口。

DB読み込み、OR-Tools最適化、結果整形、保存の各責務を順番に呼び出す。
"""

from django.db import transaction

from .models import ShiftPlan
from .shift_generation.context import load_generation_context
from .shift_generation.optimization import optimize_shift
from .shift_generation.persistence import (
    persist_generated_shift,
    save_generated_shift_results,
)
from .shift_generation.results import (
    _build_generated_shifts,
    _build_generation_violations,
    _build_optimization_summary,
    _solver_value,
    format_generation_violation_messages,
)
from .shift_generation.types import (
    ShiftGenerationError,
    ShiftGenerationResult,
    ShiftGenerationViolation,
    ShiftGenerationViolationType,
)


def generate_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """シフト表1か月分の勤務を自動生成し、結果をメモリ上で返す。"""

    context = load_generation_context(shift_plan)
    optimization = optimize_shift(context)

    shifts = _build_generated_shifts(
        solver=optimization.solver,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=optimization.shift_vars,
        fixed_assignments=context.fixed_assignments,
    )
    violations = _build_generation_violations(
        shifts=shifts,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        effective_rules=context.effective_rules,
        max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        night_shift_counts={
            staff_id: _solver_value(optimization.solver, count_var)
            for staff_id, count_var in optimization.safety_data.night_count_vars.items()
        },
    )
    optimization_summary = _build_optimization_summary(
        solver=optimization.solver,
        staffing_data=optimization.staffing_data,
        safety_data=optimization.safety_data,
        staffing_balance_data=optimization.staffing_balance_data,
        ability_balance_data=optimization.ability_balance_data,
        long_streak_terms=optimization.long_streak_terms,
        phase_results=optimization.phase_results,
    )

    return ShiftGenerationResult(
        status="success",
        shifts=shifts,
        violations=violations,
        solver_status=optimization.solver_status,
        staff_count=len(context.staff_members),
        target_day_count=len(context.month_dates),
        optimization_summary=optimization_summary,
    )


def generate_and_save_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """生成結果を ShiftResult へ保存し、ShiftPlan.status を GENERATED へ更新する。"""

    with transaction.atomic():
        result = generate_shift(shift_plan)
        persist_generated_shift(shift_plan, result)
        return result
