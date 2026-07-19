"""OR-Tools を使ったシフト自動生成ロジック。

第1段階では、月共通の休日日数と夜勤パターン、希望休・固定休・手入力固定だけを扱う。
人数充足や能力条件のような高度な条件は次段階で追加する前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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


@dataclass(frozen=True)
class GeneratedShift:
    """生成結果1件分の勤務情報。"""

    staff_member_id: int
    date: date
    shift_type: str


@dataclass(frozen=True)
class ShiftGenerationResult:
    """生成処理の結果全体。

    status:
        アプリ側で扱いやすい簡易ステータス。第1段階では成功時に `success` を返す。
    shifts:
        月内の全スタッフ・全日付について確定した勤務一覧。
    solver_status:
        OR-Tools が返したステータス名。
    staff_count / target_day_count:
        呼び出し側で確認しやすいように返す集計値。
    """

    status: str
    shifts: list[GeneratedShift]
    solver_status: str | None = None
    staff_count: int = 0
    target_day_count: int = 0


class ShiftGenerationError(Exception):
    """固定条件の矛盾やソルバー不成立を呼び出し元へ伝える例外。"""


def generate_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """シフト表1か月分の勤務を自動生成する。

    Args:
        shift_plan: 対象の `ShiftPlan`。

    Returns:
        `ShiftGenerationResult`。保存はまだ行わず、確定した勤務一覧だけを返す。

    Raises:
        ShiftGenerationError: 事前条件不足や、固定条件の矛盾、ソルバー不成立時。
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

    day_off_requests = {
        (day_off_request.staff_member_id, day_off_request.date): day_off_request
        for day_off_request in DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
    }
    fixed_results = {
        (shift_result.staff_member_id, shift_result.date): shift_result
        for shift_result in ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).filter(
            Q(input_type=ShiftResult.InputTypeChoices.MANUAL) | Q(is_locked=True)
        )
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

    model = cp_model.CpModel()
    shift_vars = _build_shift_variables(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        fixed_assignments=fixed_assignments,
    )
    _add_night_shift_eligibility_constraints(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        shift_vars=shift_vars,
    )
    _add_night_pattern_constraints(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        shift_vars=shift_vars,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
    )
    _add_monthly_off_day_constraints(
        model=model,
        staff_members=staff_members,
        month_dates=month_dates,
        shift_vars=shift_vars,
        fixed_assignments=fixed_assignments,
        off_days_per_staff=shift_rule.off_days_per_staff,
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    solver_status = solver.StatusName(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ShiftGenerationError(_build_solver_error_message(solver_status))

    return ShiftGenerationResult(
        status="success",
        shifts=_build_generated_shifts(
            solver=solver,
            staff_members=staff_members,
            month_dates=month_dates,
            shift_vars=shift_vars,
            fixed_assignments=fixed_assignments,
        ),
        solver_status=solver_status,
        staff_count=len(staff_members),
        target_day_count=len(month_dates),
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
    """夜勤 → 明け → その次の勤務、という連動制約を追加する。"""
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


def _build_solver_error_message(solver_status):
    """OR-Tools の終了状態を、画面向けに短いエラーメッセージへ変換する。"""
    if solver_status == "INFEASIBLE":
        return "固定条件が競合しているため、自動生成できませんでした。"
    if solver_status == "MODEL_INVALID":
        return "シフト生成モデルが不正な状態です。条件設定を確認してください。"
    if solver_status == "UNKNOWN":
        return "制限時間内に解を見つけられませんでした。条件を見直して再実行してください。"
    return f"シフトを自動生成できませんでした。（solver_status={solver_status}）"
