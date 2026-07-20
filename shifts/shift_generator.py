"""OR-Tools を使ったシフト自動生成ロジック。

第2段階では、第1段階のハード制約に加えて以下を追加している。

- 必要日勤人数のセミハード制約
- 必要夜勤人数のセミハード制約
- 最大連勤数のセミハード制約
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
from .services import get_effective_rule_for_date, get_month_dates


GENERATABLE_SHIFT_TYPES = (
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.OFF,
)

OFF_LIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.OFF,
    ShiftResult.ShiftTypeChoices.OFF_REQUEST,
}

WORKLIKE_SHIFT_TYPES = {
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.TRAINING,
}

SEMI_HARD_WEIGHTS = {
    "night_shortage": 100,
    "day_shortage": 80,
    "max_consecutive_work": 70,
    "night_excess": 30,
    "day_excess": 20,
}


class ShiftGenerationViolationType:
    """違反種別の定数。

    画面表示・テスト・目的関数で同じ文字列を分散させないためにまとめて持つ。
    """

    DAY_SHORTAGE = "day_shortage"
    DAY_EXCESS = "day_excess"
    NIGHT_SHORTAGE = "night_shortage"
    NIGHT_EXCESS = "night_excess"
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


@dataclass(frozen=True)
class ShiftGenerationResult:
    """生成処理の結果全体。"""

    status: str
    shifts: list[GeneratedShift]
    violations: list[ShiftGenerationViolation] = field(default_factory=list)
    solver_status: str | None = None
    staff_count: int = 0
    target_day_count: int = 0

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


@dataclass(frozen=True)
class GenerationContext:
    """モデル構築と保存処理で再利用する生成対象の読み込み結果。"""

    shift_rule: object
    month_dates: list[date]
    staff_members: list[StaffMember]
    fixed_assignments: dict[tuple[int, date], str]
    fixed_result_keys: set[tuple[int, date]]
    effective_rules: dict[date, object]


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
    )

    semi_hard_terms = []
    semi_hard_terms.extend(
        _add_staffing_semi_hard_constraints(
            model=model,
            staff_members=context.staff_members,
            month_dates=context.month_dates,
            shift_vars=shift_vars,
            effective_rules=context.effective_rules,
        )
    )
    semi_hard_terms.extend(
        _add_max_consecutive_semi_hard_constraints(
            model=model,
            staff_members=context.staff_members,
            month_dates=context.month_dates,
            shift_vars=shift_vars,
            fixed_assignments=context.fixed_assignments,
            max_consecutive_work_days=context.shift_rule.max_consecutive_work_days,
        )
    )
    if semi_hard_terms:
        model.Minimize(sum(semi_hard_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    solver_status = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ShiftGenerationError(_build_solver_error_message(solver_status))

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
    )

    return ShiftGenerationResult(
        status="success",
        shifts=shifts,
        violations=violations,
        solver_status=solver_status,
        staff_count=len(context.staff_members),
        target_day_count=len(context.month_dates),
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
    )
    effective_rules = {
        target_date: get_effective_rule_for_date(shift_plan, target_date)
        for target_date in month_dates
    }

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
    )


def _build_fixed_assignments(*, month_dates, staff_members, day_off_requests, fixed_results, regular_day_offs):
    """基礎データと固定結果をまとめて、編集不可セルの確定勤務へ変換する。"""

    fixed_assignments = {}

    for staff_member in staff_members:
        regular_days = regular_day_offs.get(staff_member.id, set())
        for target_date in month_dates:
            cell_key = (staff_member.id, target_date)
            if cell_key in day_off_requests:
                fixed_assignments[cell_key] = ShiftResult.ShiftTypeChoices.OFF_REQUEST
            elif target_date.weekday() in regular_days:
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
        if fixed_off_days > off_days_per_staff:
            raise ShiftGenerationError(
                f"{staff_member.name} は固定休だけで月休日数 {off_days_per_staff} 日を超えています。"
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
                raise ShiftGenerationError(
                    f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は月末のため明けを配置できません。"
                )

            next_shift_type = fixed_assignments.get((staff_member.id, month_dates[index + 1]))
            if (
                next_shift_type is not None
                and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            ):
                raise ShiftGenerationError(
                    f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は、翌日の固定勤務と整合しません。"
                )

            if index + 2 >= len(month_dates):
                raise ShiftGenerationError(
                    f"{staff_member.name} の {target_date:%Y-%m-%d} の夜勤は必要な後続勤務を月内に配置できません。"
                )

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
                if fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                    model.Add(night_var == 0)
                continue

            next_date = month_dates[index + 1]
            next_key = (staff_member.id, next_date)
            next_after_night_var = shift_vars[next_key][ShiftResult.ShiftTypeChoices.AFTER_NIGHT]
            model.Add(night_var == next_after_night_var)

            if index + 2 >= len(month_dates):
                if fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                    model.Add(night_var == 0)
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
):
    """月休日数を OFF / OFF_REQUEST の合計でぴったり一致させる。"""

    for staff_member in staff_members:
        fixed_off_request_count = sum(
            1
            for target_date in month_dates
            if fixed_assignments.get((staff_member.id, target_date))
            == ShiftResult.ShiftTypeChoices.OFF_REQUEST
        )
        model.Add(
            sum(
                shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.OFF]
                for target_date in month_dates
            )
            + fixed_off_request_count
            == off_days_per_staff
        )


def _add_staffing_semi_hard_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
):
    """必要人数の不足・超過を表す IntVar を作り、目的関数へ渡す。"""

    semi_hard_terms = []
    max_count = len(staff_members)

    for target_date in month_dates:
        actual_day_staff = sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.DAY]
            for staff_member in staff_members
        )
        actual_night_staff = sum(
            shift_vars[(staff_member.id, target_date)][ShiftResult.ShiftTypeChoices.NIGHT]
            for staff_member in staff_members
        )
        rule = effective_rules[target_date]

        day_shortage = model.NewIntVar(0, max_count, f"day_shortage_{target_date.isoformat()}")
        day_excess = model.NewIntVar(0, max_count, f"day_excess_{target_date.isoformat()}")
        night_shortage = model.NewIntVar(0, max_count, f"night_shortage_{target_date.isoformat()}")
        night_excess = model.NewIntVar(0, max_count, f"night_excess_{target_date.isoformat()}")

        model.Add(actual_day_staff + day_shortage - day_excess == rule.required_day_staff)
        model.Add(actual_night_staff + night_shortage - night_excess == rule.required_night_staff)

        semi_hard_terms.extend(
            [
                day_shortage * SEMI_HARD_WEIGHTS[ShiftGenerationViolationType.DAY_SHORTAGE],
                day_excess * SEMI_HARD_WEIGHTS[ShiftGenerationViolationType.DAY_EXCESS],
                night_shortage * SEMI_HARD_WEIGHTS[ShiftGenerationViolationType.NIGHT_SHORTAGE],
                night_excess * SEMI_HARD_WEIGHTS[ShiftGenerationViolationType.NIGHT_EXCESS],
            ]
        )

    return semi_hard_terms


def _add_max_consecutive_semi_hard_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    max_consecutive_work_days,
):
    """最大連勤数を超える長さのウィンドウへ違反 BoolVar を立てる。"""

    semi_hard_terms = []
    window_size = max_consecutive_work_days + 1
    if len(month_dates) < window_size:
        return semi_hard_terms

    for staff_member in staff_members:
        for start_index in range(0, len(month_dates) - window_size + 1):
            work_terms = []
            for current_date in month_dates[start_index : start_index + window_size]:
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
                f"consecutive_violation_{staff_member.id}_{month_dates[start_index].isoformat()}"
            )
            model.Add(sum(work_terms) <= max_consecutive_work_days + violation_var)
            semi_hard_terms.append(
                violation_var * SEMI_HARD_WEIGHTS[ShiftGenerationViolationType.MAX_CONSECUTIVE_WORK]
            )

    return semi_hard_terms


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
    return violations


def _build_staffing_violations(*, shift_map, staff_members, month_dates, effective_rules):
    """日勤・夜勤の必要人数との差分から違反情報を作る。"""

    violations = []

    for target_date in month_dates:
        actual_day_count = sum(
            1
            for staff_member in staff_members
            if shift_map[(staff_member.id, target_date)] == ShiftResult.ShiftTypeChoices.DAY
        )
        actual_night_count = sum(
            1
            for staff_member in staff_members
            if shift_map[(staff_member.id, target_date)] == ShiftResult.ShiftTypeChoices.NIGHT
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
        elif actual_day_count > rule.required_day_staff:
            amount = actual_day_count - rule.required_day_staff
            violations.append(
                ShiftGenerationViolation(
                    violation_type=ShiftGenerationViolationType.DAY_EXCESS,
                    message=(
                        f"{target_date.month}月{target_date.day}日の日勤が{amount}人超過しています。"
                        f" 必要人数：{rule.required_day_staff}人 / 実際の人数：{actual_day_count}人"
                    ),
                    date=target_date,
                    required_count=rule.required_day_staff,
                    actual_count=actual_day_count,
                    amount=amount,
                )
            )

        if actual_night_count < rule.required_night_staff:
            amount = rule.required_night_staff - actual_night_count
            violations.append(
                ShiftGenerationViolation(
                    violation_type=ShiftGenerationViolationType.NIGHT_SHORTAGE,
                    message=(
                        f"{target_date.month}月{target_date.day}日の夜勤が{amount}人不足しています。"
                        f" 必要人数：{rule.required_night_staff}人 / 実際の人数：{actual_night_count}人"
                    ),
                    date=target_date,
                    required_count=rule.required_night_staff,
                    actual_count=actual_night_count,
                    amount=amount,
                )
            )
        elif actual_night_count > rule.required_night_staff:
            amount = actual_night_count - rule.required_night_staff
            violations.append(
                ShiftGenerationViolation(
                    violation_type=ShiftGenerationViolationType.NIGHT_EXCESS,
                    message=(
                        f"{target_date.month}月{target_date.day}日の夜勤が{amount}人超過しています。"
                        f" 必要人数：{rule.required_night_staff}人 / 実際の人数：{actual_night_count}人"
                    ),
                    date=target_date,
                    required_count=rule.required_night_staff,
                    actual_count=actual_night_count,
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
