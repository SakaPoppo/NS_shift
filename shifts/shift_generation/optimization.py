"""OR-Toolsの制約モデル構築と複数フェーズ最適化。

ハード制約に加えて以下を追加している。

- 必要リーダー人数・必要能力スタッフ人数のハード制約
- 最大連勤数のハード制約
- 必要人数との差分を基準に日勤枠を一括均等化する目的関数
- 月間夜勤回数差などの公平性目的関数
"""

from __future__ import annotations

import logging
import math

from ortools.sat.python import cp_model

from staff.models import StaffMember

from ..models import ShiftResult
from ..services import OFF_LIKE_SHIFT_TYPES, WORKLIKE_SHIFT_TYPES
from .types import (
    GENERATABLE_SHIFT_TYPES,
    AbilityBalanceData,
    DayStaffingBalanceData,
    GenerationContext,
    NightCountBalanceData,
    OptimizationPhaseDefinition,
    OptimizationPhaseResult,
    ShiftGenerationError,
    ShiftOptimizationOutput,
)


logger = logging.getLogger(__name__)


LONG_STREAK_WEIGHTS = {"near_max": 1, "at_max": 3}
CP_SAT_INT_MAX = 2**63 - 1
PHASE_TIME_LIMITS = {
    "day_staffing_balance": 40,
    "long_streak": 15,
    "night_count_balance": 10,
    "ability_balance": 10,
}
REQUIRED_OPTIMIZATION_PHASES = {"day_staffing_balance"}
SUCCESSFUL_OPTIMIZATION_STATUSES = {"OPTIMAL", "FEASIBLE"}
BASE_STAFF_COUNT = 20
STAFF_COUNT_STEP = 10
TIME_SCALE_PER_STEP = 0.25
MAX_TIME_SCALE = 1.75


def optimize_shift(context: GenerationContext) -> ShiftOptimizationOutput:
    """読み込み済みコンテキストから制約モデルを作り、優先順に最適化する。"""

    model = cp_model.CpModel()
    shift_vars = _build_shift_variables(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        fixed_assignments=context.fixed_assignments,
    )
    _add_night_shift_eligibility_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
    )
    _add_next_month_first_regular_day_off_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
    )
    night_after_night_pattern_terms = _add_night_pattern_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        effective_rules=context.effective_rules,
    )
    _add_monthly_off_day_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        effective_off_days=context.effective_off_days,
    )

    _add_required_night_staff_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    day_staffing_balance_data = _build_day_staffing_balance_data(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    _add_staffing_safety_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    _add_max_consecutive_work_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        previous_consecutive_work_days=context.previous_consecutive_work_days,
    )
    night_count_balance_data = _build_night_count_balance_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        night_after_night_pattern_terms=night_after_night_pattern_terms,
    )
    ability_balance_data = _build_ability_total_balance_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    long_streak_terms = _add_long_consecutive_work_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        previous_consecutive_work_days=context.previous_consecutive_work_days,
    )

    phase_definitions = _build_phase_definitions(
        day_staffing_balance_data=day_staffing_balance_data,
        night_count_balance_data=night_count_balance_data,
        ability_balance_data=ability_balance_data,
        long_streak_terms=long_streak_terms,
        staff_count=len(context.staff_members),
    )
    time_scale = _calculate_solver_time_scale(len(context.staff_members))
    logger.info(
        "shift optimization configuration "
        "staff_count=%s time_scale=%.2f phase_limits=%s",
        len(context.staff_members),
        time_scale,
        {
            phase.name: phase.max_time_seconds
            for phase in phase_definitions
        },
    )
    phase_results, solver, solver_status = _run_optimization_phases(
        model=model,
        shift_vars=shift_vars,
        phase_definitions=phase_definitions,
    )

    return ShiftOptimizationOutput(
        solver=solver,
        solver_status=solver_status,
        shift_vars=shift_vars,
        day_staffing_balance_data=day_staffing_balance_data,
        night_count_balance_data=night_count_balance_data,
        ability_balance_data=ability_balance_data,
        long_streak_terms=long_streak_terms,
        phase_results=phase_results,
    )


def _build_phase_definitions(
    *,
    day_staffing_balance_data: DayStaffingBalanceData,
    night_count_balance_data: NightCountBalanceData,
    ability_balance_data: AbilityBalanceData,
    long_streak_terms: list,
    staff_count: int,
) -> list[OptimizationPhaseDefinition]:
    """現在の優先順位とスタッフ数に応じた制限時間でフェーズを作る。"""

    time_scale = _calculate_solver_time_scale(staff_count)
    objectives = [
        (
            "day_staffing_balance",
            day_staffing_balance_data.objective_score,
        ),
        ("long_streak", sum(long_streak_terms) if long_streak_terms else 0),
        ("night_count_balance", night_count_balance_data.objective_score),
        ("ability_balance", ability_balance_data.objective_score),
    ]
    return [
        OptimizationPhaseDefinition(
            name=name,
            objective=objective,
            max_time_seconds=_calculate_phase_time_limit(
                base_seconds=PHASE_TIME_LIMITS[name],
                time_scale=time_scale,
            ),
        )
        for name, objective in objectives
    ]


def _calculate_solver_time_scale(staff_count: int) -> float:
    """20人を基準に10人単位で時間を延長し、1.75倍を上限とする。"""

    if staff_count <= BASE_STAFF_COUNT:
        return 1.0
    extra_steps = math.ceil(
        (staff_count - BASE_STAFF_COUNT) / STAFF_COUNT_STEP
    )
    return min(
        1.0 + extra_steps * TIME_SCALE_PER_STEP,
        MAX_TIME_SCALE,
    )


def _calculate_phase_time_limit(*, base_seconds: int, time_scale: float) -> int:
    """倍率適用後の秒数を切り上げ、最低1秒を保証する。"""

    return max(1, math.ceil(base_seconds * time_scale))


def _add_shift_solution_hints(*, model, shift_vars: dict, solver) -> None:
    """直前の有効解に含まれるシフト配置だけを次の探索へ渡す。"""

    model.ClearHints()
    for day_vars in shift_vars.values():
        for shift_var in day_vars.values():
            model.AddHint(shift_var, solver.Value(shift_var))


def _run_optimization_phases(
    *,
    model,
    shift_vars: dict,
    phase_definitions: list[OptimizationPhaseDefinition],
) -> tuple[list[OptimizationPhaseResult], object, str]:
    """フェーズを順に解き、直前の有効な配置を次フェーズへ引き継ぐ。"""

    if not phase_definitions:
        raise ShiftGenerationError("最適化フェーズが定義されていません。")

    phase_results = []
    last_successful_solver = None
    last_successful_status = None
    last_successful_phase_name = None
    completed_successful_phase_names = set()

    for phase_index, phase in enumerate(phase_definitions):
        if last_successful_solver is not None:
            _add_shift_solution_hints(
                model=model,
                shift_vars=shift_vars,
                solver=last_successful_solver,
            )
        result = _solve_and_fix_objective(
            model=model,
            objective=phase.objective,
            phase_name=phase.name,
            max_time_seconds=phase.max_time_seconds,
        )
        phase_results.append(result)

        if result.status == "UNKNOWN":
            if (
                last_successful_solver is None
                or not REQUIRED_OPTIMIZATION_PHASES.issubset(
                    completed_successful_phase_names
                )
            ):
                raise ShiftGenerationError(
                    _build_solver_error_message(result.status)
                )
            phase_results.extend(
                OptimizationPhaseResult(
                    name=remaining_phase.name,
                    status="NOT_RUN",
                    objective_value=None,
                    optimal=False,
                    solver=None,
                )
                for remaining_phase in phase_definitions[phase_index + 1 :]
            )
            logger.warning(
                "shift optimization fallback stopped_phase=%s "
                "last_successful_phase=%s status=%s",
                phase.name,
                last_successful_phase_name,
                last_successful_status,
            )
            break

        if result.status not in SUCCESSFUL_OPTIMIZATION_STATUSES:
            raise ShiftGenerationError(
                _build_solver_error_message(result.status)
            )
        last_successful_solver = result.solver
        last_successful_status = result.status
        last_successful_phase_name = result.name
        completed_successful_phase_names.add(result.name)

    if last_successful_solver is None or last_successful_status is None:
        raise ShiftGenerationError("有効な最適化結果を取得できませんでした。")

    logger.info(
        "shift optimization completed last_successful_phase=%s status=%s",
        last_successful_phase_name,
        last_successful_status,
    )
    return phase_results, last_successful_solver, last_successful_status


def _new_solver(max_time_seconds: int) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = 8
    return solver


def _solve_and_fix_objective(
    *, model, objective, phase_name, max_time_seconds
) -> OptimizationPhaseResult:
    """1フェーズを解き、得られた最良値を後続フェーズ用の等式にする。"""

    model.Minimize(objective)
    solver = _new_solver(max_time_seconds)
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    logger.info(
        "shift optimization phase=%s status=%s elapsed=%.3fs limit=%ss",
        phase_name,
        status_name,
        solver.WallTime(),
        max_time_seconds,
    )
    if status == cp_model.UNKNOWN:
        model.ClearObjective()
        return OptimizationPhaseResult(
            name=phase_name,
            status=status_name,
            objective_value=None,
            optimal=False,
            solver=None,
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ShiftGenerationError(_build_solver_error_message(status_name))
    objective_value = int(round(solver.ObjectiveValue()))
    model.Add(objective == objective_value)
    model.ClearObjective()
    return OptimizationPhaseResult(
        name=phase_name,
        status=status_name,
        objective_value=objective_value,
        optimal=status == cp_model.OPTIMAL,
        solver=solver,
    )


def _build_shift_variables(*, model, staff_members, month_dates, fixed_assignments):
    """スタッフ×日付×勤務区分の BoolVar を作り、固定セルの基本制約も入れる。"""

    shift_vars = {}

    for staff_member in staff_members:
        for index, target_date in enumerate(month_dates):
            cell_key = (staff_member.id, target_date)
            day_vars = {
                shift_type: model.NewBoolVar(
                    f"shift_{staff_member.id}_{target_date.isoformat()}_{shift_type}"
                )
                for shift_type in GENERATABLE_SHIFT_TYPES
            }
            shift_vars[cell_key] = day_vars

            fixed_shift_type = fixed_assignments.get(cell_key)
            if fixed_shift_type is None:
                model.Add(sum(day_vars.values()) == 1)
            elif fixed_shift_type in GENERATABLE_SHIFT_TYPES:
                for shift_type, shift_var in day_vars.items():
                    model.Add(shift_var == int(shift_type == fixed_shift_type))
            else:
                # TRAINING / 有給 / 特別休暇 などは固定済みなので、生成対象の4区分はすべて 0 にする。
                model.Add(sum(day_vars.values()) == 0)

            if index == 0 and fixed_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
                model.Add(day_vars[ShiftResult.ShiftTypeChoices.AFTER_NIGHT] == 0)

    return shift_vars


def _add_night_shift_eligibility_constraints(*, model, staff_members, month_dates, shift_vars):
    """夜勤不可スタッフには NIGHT を割り当てない。"""

    for staff_member in staff_members:
        if staff_member.can_night_shift:
            continue
        for target_date in month_dates:
            model.Add(
                shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT] == 0
            )


def _add_next_month_first_regular_day_off_constraints(
    *, model, staff_members, month_dates, shift_vars
):
    """翌月1日が曜日固定休なら、当月末夜勤を候補から外す。"""
    next_month_first = month_dates[-1].fromordinal(month_dates[-1].toordinal() + 1)
    last_date = month_dates[-1]
    for staff_member in staff_members:
        regular_days = {
            day_off.day_of_week for day_off in staff_member.regular_days_off.all()
        }
        if next_month_first.weekday() in regular_days:
            model.Add(
                shift_vars[(staff_member.id, last_date)][ShiftResult.ShiftTypeChoices.NIGHT]
                == 0
            )


def _add_night_pattern_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    effective_rules,
):
    """夜勤後の勤務を連動させ、False時の明け翌日夜勤を月1回に制限する。

    False時に作成したパターン変数は、既存フェーズで回数を最小化するため返す。
    """

    night_after_night_pattern_terms = []

    for staff_member in staff_members:
        staff_pattern_terms = []
        first_date = month_dates[0]
        first_key = (staff_member.id, first_date)
        if (
            len(month_dates) >= 2
            and fixed_assignments.get(first_key)
            == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            and not effective_rules[first_date].night_shift_next_day_off
        ):
            second_date = month_dates[1]
            second_key = (staff_member.id, second_date)
            first_after_night_var = shift_vars[first_key][
                ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            ]
            second_fixed_shift_type = fixed_assignments.get(second_key)
            if second_fixed_shift_type is not None:
                if second_fixed_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {ShiftResult.ShiftTypeChoices.NIGHT}
                ):
                    model.Add(first_after_night_var == 0)
            else:
                model.Add(
                    first_after_night_var
                    <= shift_vars[second_key][ShiftResult.ShiftTypeChoices.OFF]
                    + shift_vars[second_key][ShiftResult.ShiftTypeChoices.NIGHT]
                )

            second_night_var = shift_vars[second_key][
                ShiftResult.ShiftTypeChoices.NIGHT
            ]
            boundary_pattern_var = model.NewBoolVar(
                f"night_after_night_{staff_member.id}_month_boundary"
            )
            model.Add(boundary_pattern_var <= first_after_night_var)
            model.Add(boundary_pattern_var <= second_night_var)
            model.Add(
                boundary_pattern_var
                >= first_after_night_var + second_night_var - 1
            )
            staff_pattern_terms.append(boundary_pattern_var)

        for index, target_date in enumerate(month_dates):
            current_key = (staff_member.id, target_date)
            night_var = shift_vars[current_key][ShiftResult.ShiftTypeChoices.NIGHT]
            fixed_shift_type = fixed_assignments.get(current_key)

            if index + 1 >= len(month_dates):
                continue

            next_date = month_dates[index + 1]
            next_key = (staff_member.id, next_date)
            next_after_night_var = shift_vars[next_key][ShiftResult.ShiftTypeChoices.AFTER_NIGHT]
            model.Add(night_var == next_after_night_var)

            if index + 2 >= len(month_dates):
                continue

            third_date = month_dates[index + 2]
            third_key = (staff_member.id, third_date)
            third_fixed_shift_type = fixed_assignments.get(third_key)
            rule = effective_rules[target_date]

            if rule.night_shift_next_day_off:
                if third_fixed_shift_type == ShiftResult.ShiftTypeChoices.OFF_REQUEST:
                    continue
                if third_fixed_shift_type is not None:
                    if third_fixed_shift_type != ShiftResult.ShiftTypeChoices.OFF:
                        model.Add(night_var == 0)
                    continue
                model.Add(
                    night_var
                    <= shift_vars[third_key][ShiftResult.ShiftTypeChoices.OFF]
                )
                continue

            if third_fixed_shift_type is not None:
                if third_fixed_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {ShiftResult.ShiftTypeChoices.NIGHT}
                ):
                    model.Add(night_var == 0)
            else:
                model.Add(
                    night_var
                    <= shift_vars[third_key][ShiftResult.ShiftTypeChoices.OFF]
                    + shift_vars[third_key][ShiftResult.ShiftTypeChoices.NIGHT]
                )

            third_night_var = shift_vars[third_key][
                ShiftResult.ShiftTypeChoices.NIGHT
            ]
            pattern_var = model.NewBoolVar(
                f"night_after_night_{staff_member.id}_{target_date.isoformat()}"
            )
            model.Add(pattern_var <= night_var)
            model.Add(pattern_var <= third_night_var)
            model.Add(pattern_var >= night_var + third_night_var - 1)
            staff_pattern_terms.append(pattern_var)

        if staff_pattern_terms:
            model.Add(sum(staff_pattern_terms) <= 1)
            night_after_night_pattern_terms.extend(staff_pattern_terms)

    return night_after_night_pattern_terms


def _add_monthly_off_day_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    effective_off_days,
):
    """月休日数を OFF / OFF_REQUEST の合計でぴったり一致させる。"""

    for staff_member in staff_members:
        fixed_non_generated_off_count = sum(
            1
            for target_date in month_dates
            if fixed_assignments.get((staff_member.id, target_date))
            in {
                ShiftResult.ShiftTypeChoices.OFF_REQUEST,
                ShiftResult.ShiftTypeChoices.PAID_LEAVE,
                ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
            }
        )
        model.Add(
            sum(
                shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.OFF]
                for target_date in month_dates
            )
            + fixed_non_generated_off_count
            == effective_off_days[staff_member.id]
        )


def _add_required_night_staff_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> None:
    """日ごとの必要夜勤人数を必須制約として追加する。"""

    for target_date in month_dates:
        actual_night_staff = sum(
            shift_vars[(staff_member.id, target_date)][
                ShiftResult.ShiftTypeChoices.NIGHT
            ]
            for staff_member in staff_members
        )
        model.Add(
            actual_night_staff
            == effective_rules[target_date].required_night_staff
        )


def _build_day_staffing_balance_data(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> DayStaffingBalanceData:
    """必要人数との差分と、月全体の差分幅・集計を構築する。"""

    data = DayStaffingBalanceData()
    max_count = len(staff_members)
    delta_lower_bounds = []
    delta_upper_bounds = []

    for target_date in month_dates:
        actual_day_count = model.NewIntVar(
            0, max_count, f"actual_day_count_{target_date.isoformat()}"
        )
        model.Add(
            actual_day_count
            == sum(
                shift_vars[(staff_member.id, target_date)][
                    ShiftResult.ShiftTypeChoices.DAY
                ]
                for staff_member in staff_members
            )
        )
        required_day_count = effective_rules[target_date].required_day_staff
        delta_lower_bound = -required_day_count
        delta_upper_bound = max_count - required_day_count
        delta_var = model.NewIntVar(
            delta_lower_bound,
            delta_upper_bound,
            f"day_staffing_delta_{target_date.isoformat()}",
        )
        model.Add(delta_var == actual_day_count - required_day_count)

        data.actual_day_count_vars[target_date] = actual_day_count
        data.required_day_counts[target_date] = required_day_count
        data.day_staffing_delta_vars[target_date] = delta_var
        delta_lower_bounds.append(delta_lower_bound)
        delta_upper_bounds.append(delta_upper_bound)

    minimum_delta_lower_bound = min(delta_lower_bounds)
    minimum_delta_upper_bound = min(delta_upper_bounds)
    maximum_delta_lower_bound = max(delta_lower_bounds)
    maximum_delta_upper_bound = max(delta_upper_bounds)
    data.minimum_delta = model.NewIntVar(
        minimum_delta_lower_bound,
        minimum_delta_upper_bound,
        "minimum_day_staffing_delta",
    )
    data.maximum_delta = model.NewIntVar(
        maximum_delta_lower_bound,
        maximum_delta_upper_bound,
        "maximum_day_staffing_delta",
    )
    model.AddMinEquality(
        data.minimum_delta,
        list(data.day_staffing_delta_vars.values()),
    )
    model.AddMaxEquality(
        data.maximum_delta,
        list(data.day_staffing_delta_vars.values()),
    )
    data.delta_range = model.NewIntVar(
        0,
        maximum_delta_upper_bound - minimum_delta_lower_bound,
        "day_staffing_delta_range",
    )
    model.Add(data.delta_range == data.maximum_delta - data.minimum_delta)

    data.total_actual_day_count = sum(data.actual_day_count_vars.values())
    data.total_required_day_count = sum(data.required_day_counts.values())
    data.total_delta = (
        data.total_actual_day_count - data.total_required_day_count
    )
    data.objective_score = data.delta_range
    return data


def _add_staffing_safety_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> None:
    """日ごとのリーダー人数・能力条件を必須制約として追加する。"""

    leaders = [
        staff
        for staff in staff_members
        if staff.role == StaffMember.RoleChoices.LEADER
    ]
    for target_date in month_dates:
        rule = effective_rules[target_date]
        actual_leaders = sum(
            shift_vars[(staff.id, target_date)][
                ShiftResult.ShiftTypeChoices.DAY
            ]
            for staff in leaders
        )
        model.Add(actual_leaders >= rule.required_leader_staff)

        if (
            rule.min_ability_level is None
            or rule.min_ability_level_staff_count is None
        ):
            continue
        actual_qualified_staff = sum(
            shift_vars[(staff.id, target_date)][
                ShiftResult.ShiftTypeChoices.DAY
            ]
            for staff in staff_members
            if staff.ability_level >= rule.min_ability_level
        )
        model.Add(
            actual_qualified_staff >= rule.min_ability_level_staff_count
        )


def _add_max_consecutive_work_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    max_consecutive_work_days,
    previous_consecutive_work_days,
) -> None:
    """前月からの勤務も含め、最大連勤数を超える配置を禁止する。"""

    window_size = max_consecutive_work_days + 1
    for staff_member in staff_members:
        prefix_count = min(
            previous_consecutive_work_days.get(staff_member.id, 0),
            max_consecutive_work_days,
        )
        timeline = [None] * prefix_count + month_dates
        for start_index in range(0, len(timeline) - window_size + 1):
            work_terms = [
                1
                if current_date is None
                else _work_term(
                    shift_vars,
                    fixed_assignments,
                    staff_member.id,
                    current_date,
                )
                for current_date in timeline[
                    start_index : start_index + window_size
                ]
            ]

            model.Add(sum(work_terms) <= max_consecutive_work_days)


def _work_term(shift_vars, fixed_assignments, staff_member_id, target_date):
    """最大連勤制約と長期連勤評価で同じ勤務日定義を使用する。"""
    cell_key = (staff_member_id, target_date)
    fixed_shift_type = fixed_assignments.get(cell_key)
    if fixed_shift_type is not None:
        return int(fixed_shift_type in WORKLIKE_SHIFT_TYPES)
    return (
        shift_vars[cell_key][ShiftResult.ShiftTypeChoices.DAY]
        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.NIGHT]
        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.AFTER_NIGHT]
    )


def _build_night_count_balance_objective(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    night_after_night_pattern_terms=(),
) -> NightCountBalanceData:
    """夜勤回数差を優先し、同点なら明け翌日の夜勤を減らす。"""

    data = NightCountBalanceData()
    pattern_penalty = (
        sum(night_after_night_pattern_terms)
        if night_after_night_pattern_terms
        else 0
    )
    eligible_staff = [staff for staff in staff_members if staff.can_night_shift]
    if len(eligible_staff) <= 1:
        data.night_balance_violation = 0
        data.objective_score = pattern_penalty
        return data
    for staff_member in eligible_staff:
        count_var = model.NewIntVar(0, len(month_dates), f"night_count_{staff_member.id}")
        model.Add(count_var == sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT]
            for target_date in month_dates
        ))
        data.night_count_vars[staff_member.id] = count_var
    data.night_count_min = model.NewIntVar(
        0, len(month_dates), "night_count_min"
    )
    data.night_count_max = model.NewIntVar(
        0, len(month_dates), "night_count_max"
    )
    model.AddMinEquality(
        data.night_count_min, list(data.night_count_vars.values())
    )
    model.AddMaxEquality(
        data.night_count_max, list(data.night_count_vars.values())
    )
    data.night_balance_violation = model.NewIntVar(
        0, len(month_dates), "night_balance_violation"
    )
    model.Add(
        data.night_balance_violation
        >= data.night_count_max - data.night_count_min - 1
    )
    if night_after_night_pattern_terms:
        data.objective_score = _build_lexicographic_score(
            [
                (data.night_balance_violation, len(month_dates)),
                (pattern_penalty, len(staff_members)),
            ]
        )
    else:
        data.objective_score = data.night_balance_violation
    return data


def _add_long_consecutive_work_objective(
    *, model, staff_members, month_dates, shift_vars, fixed_assignments,
    max_consecutive_work_days, previous_consecutive_work_days,
):
    terms = []
    thresholds = []
    if max_consecutive_work_days >= 2:
        thresholds.append((max_consecutive_work_days - 1, LONG_STREAK_WEIGHTS["near_max"]))
    thresholds.append((max_consecutive_work_days, LONG_STREAK_WEIGHTS["at_max"]))

    for staff_member in staff_members:
        prefix_count = min(
            previous_consecutive_work_days.get(staff_member.id, 0),
            max_consecutive_work_days,
        )
        timeline = [None] * prefix_count + month_dates
        for length, length_weight in thresholds:
            for start_index in range(len(timeline) - length + 1):
                window = timeline[start_index:start_index + length]
                work_terms = [
                    1 if target_date is None else _work_term(
                        shift_vars, fixed_assignments, staff_member.id, target_date
                    )
                    for target_date in window
                ]
                streak = model.NewBoolVar(
                    f"long_streak_{staff_member.id}_{length}_{start_index}"
                )
                # AND と同値。固定勤務も式へ含まれる。
                for work_term in work_terms:
                    model.Add(streak <= work_term)
                model.Add(streak >= sum(work_terms) - length + 1)
                terms.append(streak * length_weight)
    return terms


def _build_lexicographic_score(components):
    """(式, 上限)を優先順に並べ、安全な係数の整数スコアへ変換する。"""

    score = 0
    multiplier = 1
    score_upper_bound = 0
    for expression, upper_bound in reversed(components):
        score_upper_bound += upper_bound * multiplier
        if score_upper_bound > CP_SAT_INT_MAX:
            raise ShiftGenerationError(
                "最適化スコアがOR-Toolsの整数上限を超えるため生成できません。"
            )
        score += expression * multiplier
        multiplier *= upper_bound + 1
    return score


def _build_staffing_condition_key(rule):
    """曜日名ではなく、解決済みの勤務条件そのもので日付を分類する。"""

    return (
        rule.required_day_staff,
        rule.required_night_staff,
        rule.required_leader_staff,
        rule.min_ability_level,
        rule.min_ability_level_staff_count,
    )


def _group_dates_by_staffing_condition(month_dates, effective_rules):
    groups = {}
    for target_date in month_dates:
        key = _build_staffing_condition_key(effective_rules[target_date])
        groups.setdefault(key, []).append(target_date)
    return groups


def _add_group_ability_ranges(
    *, model, groups, ability_total_vars, ability_upper_bound, name
):
    ranges = []
    for group_index, dates in enumerate(groups.values()):
        if len(dates) < 2:
            continue
        minimum = model.NewIntVar(
            0, ability_upper_bound, f"{name}_ability_min_{group_index}"
        )
        maximum = model.NewIntVar(
            0, ability_upper_bound, f"{name}_ability_max_{group_index}"
        )
        model.AddMinEquality(
            minimum, [ability_total_vars[target_date] for target_date in dates]
        )
        model.AddMaxEquality(
            maximum, [ability_total_vars[target_date] for target_date in dates]
        )
        ability_range = model.NewIntVar(
            0, ability_upper_bound, f"{name}_ability_range_{group_index}"
        )
        model.Add(ability_range == maximum - minimum)
        ranges.append(ability_range)
    return ranges


def _build_ability_total_balance_objective(
    *, model, staff_members, month_dates, shift_vars, effective_rules
):
    data = AbilityBalanceData()
    groups = _group_dates_by_staffing_condition(month_dates, effective_rules)
    night_staff = [staff for staff in staff_members if staff.can_night_shift]
    day_upper = sum(staff.ability_level for staff in staff_members)
    night_upper = sum(staff.ability_level for staff in night_staff)
    for target_date in month_dates:
        day_total = model.NewIntVar(
            0, day_upper, f"day_ability_total_{target_date.isoformat()}"
        )
        model.Add(day_total == sum(
            staff.ability_level
            * shift_vars[(staff.id, target_date)][ShiftResult.ShiftTypeChoices.DAY]
            for staff in staff_members
        ))
        data.day_ability_total_vars[target_date] = day_total

        night_total = model.NewIntVar(
            0, night_upper, f"night_ability_total_{target_date.isoformat()}"
        )
        model.Add(night_total == sum(
            staff.ability_level
            * shift_vars[(staff.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT]
            for staff in night_staff
        ))
        data.night_ability_total_vars[target_date] = night_total

    data.day_group_ranges = _add_group_ability_ranges(
        model=model,
        groups=groups,
        ability_total_vars=data.day_ability_total_vars,
        ability_upper_bound=day_upper,
        name="day",
    )
    data.night_group_ranges = _add_group_ability_ranges(
        model=model,
        groups=groups,
        ability_total_vars=data.night_ability_total_vars,
        ability_upper_bound=night_upper,
        name="night",
    )
    data.max_day_range = _add_max_or_zero(
        model, data.day_group_ranges, day_upper, "max_day_ability_range"
    )
    data.total_day_range = sum(data.day_group_ranges)
    data.max_night_range = _add_max_or_zero(
        model, data.night_group_ranges, night_upper, "max_night_ability_range"
    )
    data.total_night_range = sum(data.night_group_ranges)
    data.objective_score = _build_lexicographic_score([
        (data.max_day_range, day_upper),
        (data.total_day_range, len(data.day_group_ranges) * day_upper),
        (data.max_night_range, night_upper),
        (data.total_night_range, len(data.night_group_ranges) * night_upper),
    ])
    return data


def _add_max_or_zero(model, variables, upper_bound, name):
    if not variables:
        return 0
    maximum = model.NewIntVar(0, upper_bound, name)
    model.AddMaxEquality(maximum, variables)
    return maximum


def _build_solver_error_message(solver_status):
    """OR-Tools の終了状態を、画面向けに短いエラーメッセージへ変換する。"""

    if solver_status == "INFEASIBLE":
        return "固定条件が競合しているため、自動生成できませんでした。"
    if solver_status == "MODEL_INVALID":
        return "シフト生成モデルが不正な状態です。条件設定を確認してください。"
    if solver_status == "UNKNOWN":
        return "制限時間内に解を見つけられませんでした。条件を見直して再実行してください。"
    return f"シフトを自動生成できませんでした。（solver_status={solver_status}）"
