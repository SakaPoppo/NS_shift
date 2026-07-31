"""OR-Toolsの制約モデル構築と複数フェーズ最適化。 

ハード制約に加えて以下を追加している。

- 必要日勤人数のセミハード制約
- 最大連勤数のセミハード制約
- 月間夜勤回数差のセミハード制約
"""

from __future__ import annotations

import logging

from ortools.sat.python import cp_model

from staff.models import StaffMember

from ..models import ShiftResult
from ..services import (
    OFF_LIKE_SHIFT_TYPES,
    WORKLIKE_SHIFT_TYPES,
)
from .types import (
    GENERATABLE_SHIFT_TYPES,
    AbilityBalanceData,
    GenerationContext,
    OptimizationPhaseResult,
    SafetyObjectiveData,
    ShiftGenerationError,
    ShiftOptimizationOutput,
    StaffingBalanceData,
    StaffingObjectiveData,
)


logger = logging.getLogger(__name__)


LONG_STREAK_WEIGHTS = {"near_max": 1, "at_max": 3}
CP_SAT_INT_MAX = 2**63 - 1
PHASE_TIME_LIMITS = {
    "total_day_shortage": 25,
    "max_day_shortage": 10,
    "safety": 15,
    "total_day_excess": 10,
    "day_count_balance": 10,
    "ability_balance": 10,
    "long_streak": 15,
}


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
    _add_night_pattern_constraints(
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

    staffing_data = _add_staffing_semi_hard_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    safety_data = _build_safety_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        effective_rules=context.effective_rules,
        max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        previous_consecutive_work_days=context.previous_consecutive_work_days,
    )
    staffing_balance_data = _build_day_count_balance_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        effective_rules=context.effective_rules,
        staffing_data=staffing_data,
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

    # 優先順位どおりに最良値を固定するため、後続フェーズが前の評価を悪化させない。
    phase_objectives = [
        ("total_day_shortage", staffing_data.total_day_shortage),
        ("max_day_shortage", staffing_data.max_day_shortage),
        ("safety", safety_data.objective_score),
        ("total_day_excess", staffing_data.total_day_excess),
        ("day_count_balance", staffing_balance_data.objective_score),
        ("ability_balance", ability_balance_data.objective_score),
        ("long_streak", sum(long_streak_terms) if long_streak_terms else 0),
    ]
    phase_results = [
        _solve_and_fix_objective(
            model=model,
            objective=objective,
            phase_name=phase_name,
            max_time_seconds=PHASE_TIME_LIMITS[phase_name],
        )
        for phase_name, objective in phase_objectives
    ]
    solver = phase_results[-1].solver
    solver_status = phase_results[-1].status

    return ShiftOptimizationOutput(
        solver=solver,
        solver_status=solver_status,
        shift_vars=shift_vars,
        staffing_data=staffing_data,
        safety_data=safety_data,
        staffing_balance_data=staffing_balance_data,
        ability_balance_data=ability_balance_data,
        long_streak_terms=long_streak_terms,
        phase_results=phase_results,
    )


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
    """夜勤→明け→その次の勤務、という連動制約を追加する。"""

    for staff_member in staff_members:
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
                if third_fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                    model.Add(night_var == 0)
                continue
            model.Add(
                night_var
                <= shift_vars[third_key][ShiftResult.ShiftTypeChoices.NIGHT]
            )


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


def _add_staffing_semi_hard_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> StaffingObjectiveData:
    """各日の必要人数を基準に不足・超過を作り、集計データとして返す。"""

    data = StaffingObjectiveData()
    max_count = len(staff_members)

    for target_date in month_dates:
        actual_day_staff = model.NewIntVar(
            0, max_count, f"actual_day_count_{target_date.isoformat()}"
        )
        model.Add(actual_day_staff == sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.DAY]
            for staff_member in staff_members
        ))
        actual_night_staff = sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT]
            for staff_member in staff_members
        )
        rule = effective_rules[target_date]

        day_shortage = model.NewIntVar(0, max_count, f"day_shortage_{target_date.isoformat()}")
        day_excess = model.NewIntVar(0, max_count, f"day_excess_{target_date.isoformat()}")
        model.Add(actual_day_staff + day_shortage - day_excess == rule.required_day_staff)
        # 夜勤人数は安全上の必須条件なので、不足・超過を許容しない。
        model.Add(actual_night_staff == rule.required_night_staff)

        data.actual_day_count_vars[target_date] = actual_day_staff
        data.day_shortage_vars[target_date] = day_shortage
        data.day_excess_vars[target_date] = day_excess

    data.total_day_shortage = sum(data.day_shortage_vars.values())
    data.total_day_excess = sum(data.day_excess_vars.values())
    data.max_day_shortage = model.NewIntVar(0, max_count, "max_day_shortage")
    model.AddMaxEquality(data.max_day_shortage, list(data.day_shortage_vars.values()))
    return data


def _add_max_consecutive_semi_hard_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    max_consecutive_work_days,
    previous_consecutive_work_days,
):
    """最大連勤数を超える長さのウィンドウへ違反 BoolVar を立てる。"""

    semi_hard_terms = []
    window_size = max_consecutive_work_days + 1
    for staff_member in staff_members:
        prefix_count = min(previous_consecutive_work_days.get(staff_member.id, 0), window_size - 1)
        timeline = [None] * prefix_count + month_dates
        for start_index in range(0, len(timeline) - window_size + 1):
            work_terms = []
            for current_date in timeline[start_index : start_index + window_size]:
                if current_date is None:
                    work_terms.append(1)
                    continue
                cell_key = (staff_member.id, current_date)
                fixed_shift_type = fixed_assignments.get(cell_key)
                if fixed_shift_type is None:
                    work_terms.append(
                        shift_vars[cell_key][ShiftResult.ShiftTypeChoices.DAY]
                        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.NIGHT]
                        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.AFTER_NIGHT]
                    )
                else:
                    work_terms.append(int(fixed_shift_type in WORKLIKE_SHIFT_TYPES))

            violation_var = model.NewBoolVar(
                f"consecutive_violation_{staff_member.id}_{start_index}"
            )
            model.Add(sum(work_terms) <= max_consecutive_work_days + violation_var)
            semi_hard_terms.append(violation_var)

    return semi_hard_terms


def _work_term(shift_vars, fixed_assignments, staff_member_id, target_date):
    """セミハード・ソフト双方で同じ勤務日定義を使用する。"""
    cell_key = (staff_member_id, target_date)
    fixed_shift_type = fixed_assignments.get(cell_key)
    if fixed_shift_type is not None:
        return int(fixed_shift_type in WORKLIKE_SHIFT_TYPES)
    return (
        shift_vars[cell_key][ShiftResult.ShiftTypeChoices.DAY]
        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.NIGHT]
        + shift_vars[cell_key][ShiftResult.ShiftTypeChoices.AFTER_NIGHT]
    )


def _add_night_count_balance_semi_hard_constraint(
    *, model, staff_members, month_dates, shift_vars
):
    eligible_staff = [staff for staff in staff_members if staff.can_night_shift]
    if len(eligible_staff) <= 1:
        return None, None, {}, None
    count_vars = {}
    for staff_member in eligible_staff:
        count_var = model.NewIntVar(0, len(month_dates), f"night_count_{staff_member.id}")
        model.Add(count_var == sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT]
            for target_date in month_dates
        ))
        count_vars[staff_member.id] = count_var
    minimum = model.NewIntVar(0, len(month_dates), "night_count_min")
    maximum = model.NewIntVar(0, len(month_dates), "night_count_max")
    model.AddMinEquality(minimum, list(count_vars.values()))
    model.AddMaxEquality(maximum, list(count_vars.values()))
    violation = model.NewIntVar(0, len(month_dates), "night_balance_violation")
    model.Add(violation >= maximum - minimum - 1)
    return minimum, maximum, count_vars, violation


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


def _build_safety_objective(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    effective_rules,
    max_consecutive_work_days,
    previous_consecutive_work_days,
):
    data = SafetyObjectiveData()
    max_count = len(staff_members)
    leaders = [
        staff for staff in staff_members
        if staff.role == StaffMember.RoleChoices.LEADER
    ]
    for target_date in month_dates:
        rule = effective_rules[target_date]
        leader_shortage = model.NewIntVar(
            0, max_count, f"leader_shortage_{target_date.isoformat()}"
        )
        actual_leaders = sum(
            shift_vars[(staff.id, target_date)][ShiftResult.ShiftTypeChoices.DAY]
            for staff in leaders
        )
        model.Add(actual_leaders + leader_shortage >= rule.required_leader_staff)
        data.leader_shortage_vars[target_date] = leader_shortage

        if (
            rule.min_ability_level is not None
            and rule.min_ability_level_staff_count is not None
        ):
            qualified_staff = [
                staff for staff in staff_members
                if staff.ability_level >= rule.min_ability_level
            ]
            qualified_shortage = model.NewIntVar(
                0, max_count, f"qualified_shortage_{target_date.isoformat()}"
            )
            actual_qualified = sum(
                shift_vars[(staff.id, target_date)][ShiftResult.ShiftTypeChoices.DAY]
                for staff in qualified_staff
            )
            model.Add(
                actual_qualified + qualified_shortage
                >= rule.min_ability_level_staff_count
            )
            data.qualified_staff_shortage_vars[target_date] = qualified_shortage

    data.consecutive_violation_vars = _add_max_consecutive_semi_hard_constraints(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        shift_vars=shift_vars,
        fixed_assignments=fixed_assignments,
        max_consecutive_work_days=max_consecutive_work_days,
        previous_consecutive_work_days=previous_consecutive_work_days,
    )
    (
        data.night_count_min,
        data.night_count_max,
        data.night_count_vars,
        data.night_balance_violation,
    ) = _add_night_count_balance_semi_hard_constraint(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        shift_vars=shift_vars,
    )
    staffing_shortage = (
        sum(data.leader_shortage_vars.values())
        + sum(data.qualified_staff_shortage_vars.values())
    )
    consecutive_violations = sum(data.consecutive_violation_vars)
    night_violation = (
        data.night_balance_violation
        if data.night_balance_violation is not None
        else 0
    )
    data.objective_score = _build_lexicographic_score([
        (staffing_shortage, len(month_dates) * max_count * 2),
        (consecutive_violations, len(data.consecutive_violation_vars)),
        (night_violation, len(month_dates)),
    ])
    return data


def _build_day_count_balance_objective(
    *, model, staff_members, month_dates, effective_rules, staffing_data
):
    data = StaffingBalanceData()
    max_count = len(staff_members)
    groups = _group_dates_by_staffing_condition(month_dates, effective_rules)
    for group_index, dates in enumerate(groups.values()):
        if len(dates) < 2:
            continue
        minimum = model.NewIntVar(0, max_count, f"group_day_min_{group_index}")
        maximum = model.NewIntVar(0, max_count, f"group_day_max_{group_index}")
        model.AddMinEquality(
            minimum, [staffing_data.actual_day_count_vars[target_date] for target_date in dates]
        )
        model.AddMaxEquality(
            maximum, [staffing_data.actual_day_count_vars[target_date] for target_date in dates]
        )
        violation = model.NewIntVar(
            0, max_count, f"group_day_balance_violation_{group_index}"
        )
        model.Add(violation >= maximum - minimum - 1)
        data.group_balance_violation_vars.append(violation)

    if data.group_balance_violation_vars:
        data.max_group_balance_violation = model.NewIntVar(
            0, max_count, "max_group_day_balance_violation"
        )
        model.AddMaxEquality(
            data.max_group_balance_violation, data.group_balance_violation_vars
        )
        data.total_group_balance_violation = sum(data.group_balance_violation_vars)
        total_upper = len(data.group_balance_violation_vars) * max_count
        data.objective_score = _build_lexicographic_score([
            (data.max_group_balance_violation, max_count),
            (data.total_group_balance_violation, total_upper),
        ])
    else:
        data.max_group_balance_violation = 0
        data.total_group_balance_violation = 0
        data.objective_score = 0
    return data


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
