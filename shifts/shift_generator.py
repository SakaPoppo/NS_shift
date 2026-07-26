"""シフト自動生成の公開窓口。

DB読み込み、OR-Tools最適化、結果整形、保存の各責務を順番に呼び出す。
"""

from django.db import transaction
from django.db.models import Q

from .models import ShiftPlan, ShiftResult
from .shift_generation.context import load_generation_context
from .shift_generation.optimization import optimize_shift
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
        save_generated_shift_results(shift_plan, result)
        shift_plan.status = ShiftPlan.StatusChoices.GENERATED
        shift_plan.save(update_fields=["status", "updated_at"])
        return result


def save_generated_shift_results(
    shift_plan: ShiftPlan, result: ShiftGenerationResult
) -> None:
    """未ロックの自動生成勤務を置き換え、生成結果を一括保存する。"""

    fixed_result_keys = {
        (shift_result.staff_member_id, shift_result.date)
        for shift_result in ShiftResult.objects.filter(
            shift_plan=shift_plan,
        ).filter(
            Q(input_type=ShiftResult.InputTypeChoices.MANUAL) | Q(is_locked=True)
        )
    }

    ShiftResult.objects.filter(
        shift_plan=shift_plan,
        input_type=ShiftResult.InputTypeChoices.GENERATED,
        is_locked=False,
    ).delete()

    create_targets = [
        ShiftResult(
            shift_plan=shift_plan,
            staff_member_id=generated_shift.staff_member_id,
            date=generated_shift.date,
            shift_type=generated_shift.shift_type,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=False,
        )
        for generated_shift in result.shifts
        if (generated_shift.staff_member_id, generated_shift.date)
        not in fixed_result_keys
    ]
    ShiftResult.objects.bulk_create(create_targets)
