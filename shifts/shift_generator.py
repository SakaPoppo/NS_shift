"""OR-Tools を使ったシフト自動生成ロジック。

ハード制約に加えて以下を追加している。

- 必要日勤人数のセミハード制約
- 最大連勤数のセミハード制約
- 月間夜勤回数差のセミハード制約
- 違反内容の組み立て
- 生成結果の DB 保存
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import Q
from ortools.sat.python import cp_model

from staff.models import StaffMember

from .models import DayOffRequest, ShiftPlan, ShiftResult
from .services import get_effective_rule_for_date, get_japanese_holiday_dates, get_month_dates


GENERATABLE_SHIFT_TYPES = (
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.OFF,
)

OFF_LIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.OFF,
    ShiftResult.ShiftTypeChoices.OFF_REQUEST,
    ShiftResult.ShiftTypeChoices.PAID_LEAVE,
    ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
}

WORKLIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.TRAINING,
}

LONG_STREAK_WEIGHTS = {"near_max": 1, "at_max": 3}
CP_SAT_INT_MAX = 2**63 - 1
PHASE_TIME_LIMITS = {
    "total_day_shortage": 12,
    "max_day_shortage": 8,
    "safety": 12,
    "total_day_excess": 8,
    "day_count_balance": 8,
    "ability_balance": 8,
    "long_streak": 10,
}


class ShiftGenerationViolationType:
    """違反種別の定数。

    画面表示・テスト・目的関数で同じ文字列を分散させないためにまとめて持つ。
    """

    DAY_SHORTAGE = "day_shortage"
    DAY_EXCESS = "day_excess"
    NIGHT_COUNT_IMBALANCE = "night_count_imbalance"
    MAX_CONSECUTIVE_WORK = "max_consecutive_work"


@dataclass(frozen=True)
class GeneratedShift:
    """生成結果1件分の勤務情報。"""

    staff_member_id: int
    date: date
    shift_type: str


@dataclass(frozen=True)
class ShiftGenerationViolation:
    """生成後に組み立てる表示用の違反情報。"""

    violation_type: str
    message: str
    date: date | None = None
    staff_member_id: int | None = None
    required_count: int | None = None
    actual_count: int | None = None
    amount: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    minimum_count: int | None = None
    maximum_count: int | None = None
    count_difference: int | None = None
    allowed_difference: int | None = None


@dataclass(frozen=True)
class ShiftOptimizationSummary:
    total_day_shortage: int
    max_day_shortage: int
    leader_shortage_total: int
    qualified_staff_shortage_total: int
    max_consecutive_violation_count: int
    night_shift_count_min: int | None
    night_shift_count_max: int | None
    night_count_imbalance_violation: int
    total_day_excess: int
    max_day_count_balance_violation: int
    total_day_count_balance_violation: int
    max_day_ability_total_range: int
    total_day_ability_total_range: int
    max_night_ability_total_range: int
    total_night_ability_total_range: int
    long_streak_penalty: int
    phase_statuses: dict[str, str] = field(default_factory=dict)
    phase_optimal_flags: dict[str, bool] = field(default_factory=dict)
    night_shift_counts: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ShiftGenerationResult:
    """生成処理の結果全体。"""

    status: str
    shifts: list[GeneratedShift]
    violations: list[ShiftGenerationViolation] = field(default_factory=list)
    solver_status: str | None = None
    staff_count: int = 0
    target_day_count: int = 0
    optimization_summary: ShiftOptimizationSummary | None = None

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


@dataclass
class StaffingObjectiveData:
    """日別の必要日勤人数に対する不足・超過をまとめて保持する。"""

    actual_day_count_vars: dict[date, object] = field(default_factory=dict)
    day_shortage_vars: dict[date, object] = field(default_factory=dict)
    day_excess_vars: dict[date, object] = field(default_factory=dict)
    total_day_shortage: object | None = None
    total_day_excess: object | None = None
    max_day_shortage: object | None = None
    max_day_excess: object | None = None


@dataclass
class SafetyObjectiveData:
    leader_shortage_vars: dict[date, object] = field(default_factory=dict)
    qualified_staff_shortage_vars: dict[date, object] = field(default_factory=dict)
    consecutive_violation_vars: list = field(default_factory=list)
    night_count_min: object | None = None
    night_count_max: object | None = None
    night_count_vars: dict[int, object] = field(default_factory=dict)
    night_balance_violation: object | None = None
    objective_score: object | None = None


@dataclass
class StaffingBalanceData:
    group_balance_violation_vars: list = field(default_factory=list)
    max_group_balance_violation: object | None = None
    total_group_balance_violation: object | None = None
    objective_score: object | None = None


@dataclass
class AbilityBalanceData:
    day_ability_total_vars: dict[date, object] = field(default_factory=dict)
    night_ability_total_vars: dict[date, object] = field(default_factory=dict)
    day_group_ranges: list = field(default_factory=list)
    night_group_ranges: list = field(default_factory=list)
    max_day_range: object | None = None
    total_day_range: object | None = None
    max_night_range: object | None = None
    total_night_range: object | None = None
    objective_score: object | None = None


@dataclass(frozen=True)
class OptimizationPhaseResult:
    name: str
    status: str
    objective_value: int
    optimal: bool
    solver: object


@dataclass(frozen=True)
class GenerationContext:
    """モデル構築と保存処理で再利用する生成対象の読み込み結果。"""

    shift_rule: object
    month_dates: list[date]
    staff_members: list[StaffMember]
    fixed_assignments: dict[tuple[int, date], str]
    fixed_result_keys: set[tuple[int, date]]
    effective_rules: dict[date, object]
    previous_consecutive_work_days: dict[int, int]
    effective_off_days: dict[int, int]


class ShiftGenerationError(Exception):
    """固定条件の矛盾やソルバー不成立を呼び出し元へ伝える例外。"""


def generate_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """シフト表1か月分の勤務を自動生成し、結果をメモリ上で返す。"""

    context = load_generation_context(shift_plan)

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
        off_days_per_staff=context.shift_rule.off_days_per_staff,
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

    shifts = _build_generated_shifts(
        solver=solver,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
    )
    violations = _build_generation_violations(
        shifts=shifts,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        effective_rules=context.effective_rules,
        max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        night_shift_counts={
            staff_id: _solver_value(solver, count_var)
            for staff_id, count_var in safety_data.night_count_vars.items()
        },
    )
    optimization_summary = _build_optimization_summary(
        solver=solver,
        staffing_data=staffing_data,
        safety_data=safety_data,
        staffing_balance_data=staffing_balance_data,
        ability_balance_data=ability_balance_data,
        long_streak_terms=long_streak_terms,
        phase_results=phase_results,
    )

    return ShiftGenerationResult(
        status="success",
        shifts=shifts,
        violations=violations,
        solver_status=solver_status,
        staff_count=len(context.staff_members),
        target_day_count=len(context.month_dates),
        optimization_summary=optimization_summary,
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


def generate_and_save_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """生成結果を ShiftResult へ保存し、ShiftPlan.status を GENERATED へ更新する。"""

    with transaction.atomic():
        result = generate_shift(shift_plan)
        save_generated_shift_results(shift_plan, result)
        shift_plan.status = ShiftPlan.StatusChoices.GENERATED
        shift_plan.save(update_fields=["status", "updated_at"])
        return result


def save_generated_shift_results(shift_plan: ShiftPlan, result: ShiftGenerationResult) -> None:
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
        if (generated_shift.staff_member_id, generated_shift.date) not in fixed_result_keys
    ]
    ShiftResult.objects.bulk_create(create_targets)


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


def load_generation_context(shift_plan: ShiftPlan) -> GenerationContext:
    """生成に必要な DB データをまとめて読み込む。

    戻り値へ集約しておくことで、モデル構築側と保存側の責務を分離しやすくする。
    """

    shift_rule = getattr(shift_plan, "shift_rule", None)
    if shift_rule is None:
        raise ShiftGenerationError("シフト条件が未設定のため、自動生成を開始できません。")

    month_dates = get_month_dates(shift_plan.year, shift_plan.month)
    holiday_dates = get_japanese_holiday_dates(shift_plan.year, shift_plan.month)
    staff_members = list(
        StaffMember.objects.filter(
            user=shift_plan.user,
            is_active=True,
        ).prefetch_related("regular_days_off")
    )
    if not staff_members:
        raise ShiftGenerationError("有効なスタッフがいないため、自動生成できません。")

    weekday_rules = list(shift_plan.weekday_rules.all())
    date_rules = list(shift_plan.date_rules.all())
    shift_plan._weekday_rule_map = {rule.day_of_week: rule for rule in weekday_rules}
    shift_plan._date_rule_map = {rule.target_date: rule for rule in date_rules}

    day_off_requests = {
        (day_off_request.staff_member_id, day_off_request.date): day_off_request
        for day_off_request in DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
    }
    fixed_result_queryset = ShiftResult.objects.filter(
        shift_plan=shift_plan,
        staff_member__in=staff_members,
    ).filter(
        Q(input_type=ShiftResult.InputTypeChoices.MANUAL) | Q(is_locked=True)
    )
    fixed_results = {
        (shift_result.staff_member_id, shift_result.date): shift_result
        for shift_result in fixed_result_queryset
    }
    regular_day_offs = {
        staff_member.id: {
            day_off.day_of_week for day_off in staff_member.regular_days_off.all()
        }
        for staff_member in staff_members
    }
    fixed_assignments = _build_fixed_assignments(
        month_dates=month_dates,
        staff_members=staff_members,
        day_off_requests=day_off_requests,
        fixed_results=fixed_results,
        regular_day_offs=regular_day_offs,
        holiday_dates=holiday_dates,
    )
    effective_rules = {
        target_date: get_effective_rule_for_date(shift_plan, target_date)
        for target_date in month_dates
    }
    night_capable_count = sum(staff_member.can_night_shift for staff_member in staff_members)
    for target_date, effective_rule in effective_rules.items():
        if effective_rule.required_night_staff > night_capable_count:
            raise ShiftGenerationError(
                f"{target_date.month}月{target_date.day}日は夜勤が"
                f"{effective_rule.required_night_staff}人必要ですが、夜勤を配置できるスタッフが"
                f"{night_capable_count}人しかいません。"
            )

    previous_consecutive_work_days = {
        carryover.staff_member_id: carryover.previous_consecutive_work_days
        for carryover in shift_plan.carryovers.filter(staff_member__in=staff_members)
    }
    mandatory_off_counts = {
        staff_member.id: len({
            target_date for target_date in month_dates
            if target_date.weekday() in regular_day_offs.get(staff_member.id, set())
            or (staff_member.is_holiday_off and target_date in holiday_dates)
            or (
                fixed_results.get((staff_member.id, target_date)) is not None
                and fixed_results[(staff_member.id, target_date)].lock_reason
                == ShiftResult.LockReasonChoices.MONTH_BOUNDARY
                and fixed_results[(staff_member.id, target_date)].shift_type
                == ShiftResult.ShiftTypeChoices.OFF
            )
        })
        for staff_member in staff_members
    }
    effective_off_days = {
        staff_id: max(shift_rule.off_days_per_staff, count)
        for staff_id, count in mandatory_off_counts.items()
    }
    for staff_member in staff_members:
        fixed_off_count = sum(
            fixed_assignments.get((staff_member.id, target_date)) in OFF_LIKE_SHIFT_TYPES
            for target_date in month_dates
        )
        if fixed_off_count > effective_off_days[staff_member.id]:
            raise ShiftGenerationError(
                f"{staff_member.name} は月休日数 {effective_off_days[staff_member.id]} 日を超えています。"
                "月休日数または希望休・有給などを見直してください。"
            )

    _validate_fixed_assignments(
        staff_members=staff_members,
        month_dates=month_dates,
        fixed_assignments=fixed_assignments,
        shift_rule=shift_rule,
        effective_rules=effective_rules,
    )

    return GenerationContext(
        shift_rule=shift_rule,
        month_dates=month_dates,
        staff_members=staff_members,
        fixed_assignments=fixed_assignments,
        fixed_result_keys=set(fixed_results),
        effective_rules=effective_rules,
        previous_consecutive_work_days=previous_consecutive_work_days,
        effective_off_days=effective_off_days,
    )


def _build_fixed_assignments(
    *, month_dates, staff_members, day_off_requests, fixed_results,
    regular_day_offs, holiday_dates,
):
    """基礎データと固定結果をまとめて、編集不可セルの確定勤務へ変換する。"""

    fixed_assignments = {}

    for staff_member in staff_members:
        regular_days = regular_day_offs.get(staff_member.id, set())
        for target_date in month_dates:
            cell_key = (staff_member.id, target_date)
            if cell_key in day_off_requests:
                fixed_assignments[cell_key] = ShiftResult.ShiftTypeChoices.OFF_REQUEST
            elif (
                target_date.weekday() in regular_days
                or (staff_member.is_holiday_off and target_date in holiday_dates)
            ):
                fixed_assignments[cell_key] = ShiftResult.ShiftTypeChoices.OFF

    for cell_key, shift_result in fixed_results.items():
        existing_shift_type = fixed_assignments.get(cell_key)
        if existing_shift_type is not None and existing_shift_type != shift_result.shift_type:
            staff_member_id, target_date = cell_key
            raise ShiftGenerationError(
                f"{target_date:%Y-%m-%d} のスタッフID {staff_member_id} は、"
                f"固定条件「{existing_shift_type}」と保存済み勤務「{shift_result.shift_type}」が競合しています。"
            )
        fixed_assignments[cell_key] = shift_result.shift_type

    return fixed_assignments


def _validate_fixed_assignments(*, staff_members, month_dates, fixed_assignments, shift_rule, effective_rules):
    """固定済み勤務だけで確定している矛盾を、ソルバー投入前に検出する。"""

    staff_by_id = {staff_member.id: staff_member for staff_member in staff_members}
    off_days_per_staff = shift_rule.off_days_per_staff

    for staff_member in staff_members:
        fixed_off_days = sum(
            1
            for target_date in month_dates
            if fixed_assignments.get((staff_member.id, target_date)) in OFF_LIKE_SHIFT_TYPES
        )
        for index, target_date in enumerate(month_dates):
            fixed_shift_type = fixed_assignments.get((staff_member.id, target_date))
            if fixed_shift_type is None:
                continue

            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.NIGHT
                and not staff_member.can_night_shift
            ):
                raise ShiftGenerationError(
                    f"{staff_member.name} は夜勤不可ですが、{target_date:%Y-%m-%d} に夜勤が固定されています。"
                )

            if fixed_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT and index > 0:
                previous_shift_type = fixed_assignments.get((staff_member.id, month_dates[index - 1]))
                if previous_shift_type is not None and previous_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                    raise ShiftGenerationError(
                        f"{staff_member.name} の {target_date:%Y-%m-%d} の明けは、前日の固定勤務と整合しません。"
                    )

            if fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                continue

            if index + 1 >= len(month_dates):
                continue

            next_shift_type = fixed_assignments.get((staff_member.id, month_dates[index + 1]))
            if (
                next_shift_type is not None
                and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            ):
                raise ShiftGenerationError(
                    f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は、翌日の固定勤務と整合しません。"
                )

            if index + 2 >= len(month_dates):
                continue

            third_shift_type = fixed_assignments.get((staff_member.id, month_dates[index + 2]))
            rule = effective_rules[target_date]
            if rule.night_shift_next_day_off:
                if third_shift_type is not None and third_shift_type not in OFF_LIKE_SHIFT_TYPES:
                    raise ShiftGenerationError(
                        f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は、2日後の固定勤務と整合しません。"
                    )
            elif (
                third_shift_type is not None
                and third_shift_type != ShiftResult.ShiftTypeChoices.NIGHT
            ):
                raise ShiftGenerationError(
                    f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は、2日後の固定勤務と整合しません。"
                )

    unknown_staff_ids = {staff_member_id for staff_member_id, _ in fixed_assignments} - set(staff_by_id)
    if unknown_staff_ids:
        raise ShiftGenerationError("固定勤務に対象外スタッフが含まれているため、自動生成できません。")


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
    off_days_per_staff,
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
    data.max_day_excess = model.NewIntVar(0, max_count, "max_day_excess")
    model.AddMaxEquality(data.max_day_shortage, list(data.day_shortage_vars.values()))
    model.AddMaxEquality(data.max_day_excess, list(data.day_excess_vars.values()))
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


def _solver_value(solver, expression) -> int:
    return int(solver.Value(expression)) if expression is not None else 0


def _build_optimization_summary(
    *,
    solver,
    staffing_data,
    safety_data,
    staffing_balance_data,
    ability_balance_data,
    long_streak_terms,
    phase_results,
):
    return ShiftOptimizationSummary(
        total_day_shortage=_solver_value(solver, staffing_data.total_day_shortage),
        max_day_shortage=_solver_value(solver, staffing_data.max_day_shortage),
        leader_shortage_total=sum(
            _solver_value(solver, var)
            for var in safety_data.leader_shortage_vars.values()
        ),
        qualified_staff_shortage_total=sum(
            _solver_value(solver, var)
            for var in safety_data.qualified_staff_shortage_vars.values()
        ),
        max_consecutive_violation_count=sum(
            _solver_value(solver, var)
            for var in safety_data.consecutive_violation_vars
        ),
        night_shift_count_min=(
            _solver_value(solver, safety_data.night_count_min)
            if safety_data.night_count_min is not None else None
        ),
        night_shift_count_max=(
            _solver_value(solver, safety_data.night_count_max)
            if safety_data.night_count_max is not None else None
        ),
        night_count_imbalance_violation=_solver_value(
            solver, safety_data.night_balance_violation
        ),
        total_day_excess=_solver_value(solver, staffing_data.total_day_excess),
        max_day_count_balance_violation=_solver_value(
            solver, staffing_balance_data.max_group_balance_violation
        ),
        total_day_count_balance_violation=_solver_value(
            solver, staffing_balance_data.total_group_balance_violation
        ),
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
        phase_optimal_flags={result.name: result.optimal for result in phase_results},
        night_shift_counts={
            staff_id: _solver_value(solver, count_var)
            for staff_id, count_var in safety_data.night_count_vars.items()
        },
    )


def _build_generated_shifts(*, solver, staff_members, month_dates, shift_vars, fixed_assignments):
    """ソルバー解と固定セルをまとめ、返却用の勤務一覧へ整形する。"""

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
    """解から表示用の違反一覧を組み立てる。"""

    shift_map = {
        (generated_shift.staff_member_id, generated_shift.date): generated_shift.shift_type
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
    return [ShiftGenerationViolation(
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
    )]


def _build_staffing_violations(*, shift_map, staff_members, month_dates, effective_rules):
    """日勤・夜勤の必要人数との差分から違反情報を作る。"""

    violations = []

    for target_date in month_dates:
        actual_day_count = sum(
            1
            for staff_member in staff_members
            if shift_map[(staff_member.id, target_date)] == ShiftResult.ShiftTypeChoices.DAY
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
    *,
    shift_map,
    staff_members,
    month_dates,
    max_consecutive_work_days,
):
    """連続勤務の塊を見つけ、最大連勤数超過を1塊につき1件だけ返す。"""

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


def _build_single_consecutive_violation(*, staff_member, run_start, run_end, max_consecutive_work_days):
    """1つの連勤区間から、必要なら違反1件だけを作る。"""

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


def _build_solver_error_message(solver_status):
    """OR-Tools の終了状態を、画面向けに短いエラーメッセージへ変換する。"""

    if solver_status == "INFEASIBLE":
        return "固定条件が競合しているため、自動生成できませんでした。"
    if solver_status == "MODEL_INVALID":
        return "シフト生成モデルが不正な状態です。条件設定を確認してください。"
    if solver_status == "UNKNOWN":
        return "制限時間内に解を見つけられませんでした。条件を見直して再実行してください。"
    return f"シフトを自動生成できませんでした。（solver_status={solver_status}）"
