"""シフト表編集画面で使う、HTTP 非依存の勤務編集ロジック。"""

from .models import ShiftCarryover, ShiftResult
from .services import OFF_LIKE_SHIFT_TYPES

# ShiftResult の memo は現在の編集画面では利用していないため、夜勤入力により
# 連動設定したセルだけを安全に識別する内部マーカーとして使用する。これにより、
# 夜勤を解除したときにユーザーが別途設定した「休」を消さずに済む。
NIGHT_SHIFT_AUTO_MEMO = "__night_shift_auto__"

SHIFT_TYPE_LABELS = dict(ShiftResult.ShiftTypeChoices.choices)
BASE_FIXED_SOURCE_LABELS = {
    "day_off_request": "希望休",
    "regular_day_off": "曜日固定休",
    "holiday_off": "祝日固定休",
    "month_boundary": "前月勤務の引き継ぎ",
    "trial_fixed": "固定勤務",
}


def parse_submitted_assignments(
    post_data,
    staff_members,
    month_dates,
    base_fixed_assignments,
):
    """POST された勤務入力を、保存候補の辞書へ変換する。

    希望休・固定休セルは HTML だけでなくサーバー側でも除外し、POST されても無視する。
    """
    valid_shift_types = {choice[0] for choice in ShiftResult.ShiftTypeChoices.choices}
    submitted_assignments = {}

    for staff_member in staff_members:
        for current_date in month_dates:
            cell_key = (staff_member.id, current_date)
            if cell_key in base_fixed_assignments:
                continue

            field_name = f"shift_{staff_member.id}_{current_date.isoformat()}"
            if field_name not in post_data:
                continue
            selected_value = post_data.get(field_name, "").strip()
            if selected_value and selected_value not in valid_shift_types:
                selected_value = ""
            submitted_assignments[cell_key] = selected_value

    return submitted_assignments


def apply_night_shift_sequences(
    shift_plan,
    month_dates,
    submitted_assignments,
    base_fixed_assignments,
    existing_results_by_key,
    *,
    shift_rule=None,
):
    """夜勤の入力・解除に合わせて、画面内の後続セルを連動させる。

    戻り値は今回自動設定したセルのキー。保存時に内部マーカーを付け、次回の
    夜勤解除時にもユーザー入力と区別できるようにする。
    """
    month_dates_set = set(month_dates)
    shift_rule = shift_rule or shift_plan.shift_rule
    auto_assignment_keys = set()
    night_after_conflicts = set()

    def get_date_with_offset(current_date, offset):
        target_date = current_date.fromordinal(current_date.toordinal() + offset)
        return target_date if target_date in month_dates_set else None

    def clear_auto_followups(staff_member_id, night_date):
        offsets = [1, 2] if shift_rule.night_shift_next_day_off else [1]
        for offset in offsets:
            target_date = get_date_with_offset(night_date, offset)
            if target_date is None:
                continue
            target_key = (staff_member_id, target_date)
            existing_result = existing_results_by_key.get(target_key)
            submitted_value = submitted_assignments.get(target_key)
            if (
                existing_result
                and existing_result.memo == NIGHT_SHIFT_AUTO_MEMO
                and (
                    target_key not in submitted_assignments
                    or submitted_value == existing_result.shift_type
                )
            ):
                submitted_assignments[target_key] = ""

    # 既存の夜勤を別勤務へ変更した場合、同じ操作で変更されていない自動入力だけを
    # 解除する。明示的に変更されたセルはユーザーの新しい入力として残す。
    for cell_key, selected_value in list(submitted_assignments.items()):
        existing_result = existing_results_by_key.get(cell_key)
        if (
            existing_result
            and existing_result.shift_type == ShiftResult.ShiftTypeChoices.NIGHT
            and selected_value != ShiftResult.ShiftTypeChoices.NIGHT
        ):
            clear_auto_followups(*cell_key)

    # 自動設定した明けは単独で変更させない。親の夜勤を変更した場合だけ明けを
    # 解除し、それ以外の明けセルへのPOST値は保存済みの自動値へ戻す。
    for cell_key, selected_value in list(submitted_assignments.items()):
        existing_result = existing_results_by_key.get(cell_key)
        if (
            not existing_result
            or existing_result.memo != NIGHT_SHIFT_AUTO_MEMO
            or selected_value == existing_result.shift_type
        ):
            continue
        staff_member_id, target_date = cell_key
        for offset in (1, 2):
            night_date = get_date_with_offset(target_date, -offset)
            if night_date is None:
                continue
            night_key = (staff_member_id, night_date)
            night_result = existing_results_by_key.get(night_key)
            if night_result and night_result.shift_type == ShiftResult.ShiftTypeChoices.NIGHT:
                parent_night_value = submitted_assignments.get(
                    night_key,
                    night_result.shift_type,
                )
                submitted_assignments[cell_key] = (
                    ""
                    if parent_night_value != ShiftResult.ShiftTypeChoices.NIGHT
                    else existing_result.shift_type
                )
                break

    # 新たに夜勤を入力した日から、翌日の明けだけを自動設定する。明けセルが
    # 保存済み・固定・画面上の未保存入力で埋まっている場合は書き換えない。
    for (staff_member_id, night_date), selected_value in list(submitted_assignments.items()):
        if selected_value != ShiftResult.ShiftTypeChoices.NIGHT:
            continue
        existing_night = existing_results_by_key.get((staff_member_id, night_date))
        if (
            existing_night
            and existing_night.shift_type == ShiftResult.ShiftTypeChoices.NIGHT
        ):
            continue

        after_night_date = get_date_with_offset(night_date, 1)
        if after_night_date is None:
            continue
        after_night_key = (staff_member_id, after_night_date)
        existing_after_night = existing_results_by_key.get(after_night_key)
        submitted_after_night = submitted_assignments.get(after_night_key, "")
        if (
            after_night_key in base_fixed_assignments
            or existing_after_night is not None
            or submitted_after_night not in ("", ShiftResult.ShiftTypeChoices.AFTER_NIGHT)
        ):
            night_after_conflicts.add((staff_member_id, night_date))
            continue
        submitted_assignments[after_night_key] = ShiftResult.ShiftTypeChoices.AFTER_NIGHT
        auto_assignment_keys.add(after_night_key)

    return auto_assignment_keys, night_after_conflicts


def build_final_shift_types(
    staff_members,
    month_dates,
    existing_results_by_key,
    base_fixed_assignments,
    submitted_assignments,
):
    """保存後に成立する勤務状態を、検証用に仮組みする。"""
    final_shift_types = {}

    for staff_member in staff_members:
        for current_date in month_dates:
            cell_key = (staff_member.id, current_date)
            if cell_key in base_fixed_assignments:
                final_shift_types[cell_key] = base_fixed_assignments[cell_key]["shift_type"]
                continue

            if cell_key in submitted_assignments:
                final_shift_types[cell_key] = submitted_assignments[cell_key]
                continue

            existing_result = existing_results_by_key.get(cell_key)
            final_shift_types[cell_key] = existing_result.shift_type if existing_result else ""

    return final_shift_types


def validate_manual_assignments(
    shift_plan,
    staff_members,
    month_dates,
    submitted_assignments,
    base_fixed_assignments,
    existing_results_by_key,
    night_after_conflicts=frozenset(),
    *,
    previous_shift_types=None,
    shift_rule=None,
):
    """夜勤と明けの前後関係を検証する。"""
    month_dates_set = set(month_dates)
    final_shift_types = build_final_shift_types(
        staff_members,
        month_dates,
        existing_results_by_key,
        base_fixed_assignments,
        submitted_assignments,
    )
    if previous_shift_types is None:
        previous_shift_types = dict(
            ShiftCarryover.objects.filter(
                shift_plan=shift_plan,
                source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            ).values_list("staff_member_id", "previous_last_shift_type")
        )
    shift_rule = shift_rule or shift_plan.shift_rule
    changed_keys = set()

    for cell_key, selected_value in submitted_assignments.items():
        existing_result = existing_results_by_key.get(cell_key)
        existing_value = existing_result.shift_type if existing_result else ""
        if selected_value != existing_value:
            changed_keys.add(cell_key)

    validation_targets = set(changed_keys)
    for staff_member_id, current_date in changed_keys:
        for offset in (-1, 1):
            adjacent_date = current_date.fromordinal(current_date.toordinal() + offset)
            if adjacent_date in month_dates_set:
                validation_targets.add((staff_member_id, adjacent_date))

    errors = []
    for staff_member_id, current_date in sorted(
        validation_targets,
        key=lambda item: (item[0], item[1]),
    ):
        current_key = (staff_member_id, current_date)
        current_shift_type = final_shift_types.get(current_key, "")

        if current_shift_type == ShiftResult.ShiftTypeChoices.NIGHT:
            if current_key in night_after_conflicts:
                errors.append(
                    f"{current_date.day}日の夜勤は保存できません。"
                    "翌日の明けセルに勤務が入力されています。"
                )
                continue

            next_date = current_date.fromordinal(current_date.toordinal() + 1)
            if next_date not in month_dates_set:
                continue

            next_key = (staff_member_id, next_date)
            base_fixed = base_fixed_assignments.get(next_key)
            if base_fixed:
                source_label = BASE_FIXED_SOURCE_LABELS[base_fixed["source"]]
                errors.append(
                    f"{current_date.day}日の夜勤は保存できません。"
                    f"翌日の{next_date.day}日が{source_label}のため、夜勤明けを配置できません。"
                )
                continue

            next_shift_type = final_shift_types.get(next_key, "")
            if next_shift_type and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
                errors.append(
                    f"{current_date.day}日の夜勤は保存できません。"
                    f"翌日の{next_date.day}日に「{SHIFT_TYPE_LABELS.get(next_shift_type, next_shift_type)}」"
                    "が入っているため、夜勤明けを配置できません。"
                )

        if current_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT:
            previous_date = current_date.fromordinal(current_date.toordinal() - 1)
            if previous_date not in month_dates_set:
                if staff_member_id not in previous_shift_types:
                    continue
                if previous_shift_types[staff_member_id] == ShiftResult.ShiftTypeChoices.NIGHT:
                    if not shift_rule.night_shift_next_day_off:
                        next_date = current_date.fromordinal(current_date.toordinal() + 1)
                        next_shift_type = final_shift_types.get(
                            (staff_member_id, next_date),
                            "",
                        )
                        if (
                            next_shift_type
                            and next_shift_type
                            not in (
                                OFF_LIKE_SHIFT_TYPES
                                | {ShiftResult.ShiftTypeChoices.NIGHT}
                            )
                        ):
                            errors.append(
                                f"{next_date.day}日の勤務は保存できません。"
                                "前月末の夜勤明け翌日は、休みまたは夜勤にしてください。"
                            )
                    continue
                errors.append(
                    f"{current_date.day}日の明けは保存できません。"
                    f"前日の{previous_date.day}日に夜勤が存在しません。"
                )
                continue

            previous_key = (staff_member_id, previous_date)
            base_fixed = base_fixed_assignments.get(previous_key)
            if base_fixed:
                source_label = BASE_FIXED_SOURCE_LABELS[base_fixed["source"]]
                errors.append(
                    f"{current_date.day}日の明けは保存できません。"
                    f"前日の{previous_date.day}日が{source_label}のため、夜勤が存在しません。"
                )
                continue

            previous_shift_type = final_shift_types.get(previous_key, "")
            if previous_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                if previous_shift_type:
                    errors.append(
                        f"{current_date.day}日の明けは保存できません。"
                        f"前日の{previous_date.day}日に「{SHIFT_TYPE_LABELS.get(previous_shift_type, previous_shift_type)}」"
                        "が入っているため、夜勤の翌日になっていません。"
                    )
                else:
                    errors.append(
                        f"{current_date.day}日の明けは保存できません。"
                        f"前日の{previous_date.day}日に夜勤が存在しません。"
                    )

    deduped_errors = []
    seen = set()
    for error in errors:
        if error in seen:
            continue
        seen.add(error)
        deduped_errors.append(error)
    return deduped_errors


def save_manual_shift_results(
    shift_plan,
    submitted_assignments,
    existing_results_by_key,
    auto_assignment_keys=frozenset(),
):
    """検証済みの入力だけを ShiftResult へ反映する。"""
    for (staff_member_id, current_date), selected_value in submitted_assignments.items():
        existing_result = existing_results_by_key.get((staff_member_id, current_date))
        is_night_shift_auto = (staff_member_id, current_date) in auto_assignment_keys

        if not selected_value:
            if existing_result:
                existing_result.delete()
            continue

        if existing_result:
            if (
                selected_value == existing_result.shift_type
                and is_night_shift_auto
                and existing_result.memo != NIGHT_SHIFT_AUTO_MEMO
            ):
                existing_result.input_type = ShiftResult.InputTypeChoices.MANUAL
                existing_result.memo = NIGHT_SHIFT_AUTO_MEMO
                existing_result.save(update_fields=["input_type", "memo", "updated_at"])
                continue
            if selected_value == existing_result.shift_type:
                continue
            existing_result.shift_type = selected_value
            existing_result.input_type = ShiftResult.InputTypeChoices.MANUAL
            if existing_result.memo == NIGHT_SHIFT_AUTO_MEMO or is_night_shift_auto:
                existing_result.memo = NIGHT_SHIFT_AUTO_MEMO if is_night_shift_auto else ""
            existing_result.save(
                update_fields=["shift_type", "input_type", "memo", "updated_at"]
            )
            continue

        ShiftResult.objects.create(
            shift_plan=shift_plan,
            staff_member_id=staff_member_id,
            date=current_date,
            shift_type=selected_value,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
            memo=NIGHT_SHIFT_AUTO_MEMO if is_night_shift_auto else "",
        )


def reset_generated_results(shift_plan):
    """自動生成勤務だけを削除し、手入力勤務は残す。"""
    ShiftResult.objects.filter(
        shift_plan=shift_plan,
        input_type=ShiftResult.InputTypeChoices.GENERATED,
        is_locked=False,
    ).delete()


def reset_all_shift_results(shift_plan):
    """ShiftResult を全削除し、希望休・固定休だけの状態へ戻す。"""
    ShiftResult.objects.filter(shift_plan=shift_plan).exclude(
        lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY
    ).delete()
