"""生成結果のDB保存処理。"""

from django.db.models import Q

from ..models import ShiftPlan, ShiftResult
from .types import ShiftGenerationResult


def save_generated_shift_results(
    shift_plan: ShiftPlan, result: ShiftGenerationResult
) -> None:
    """未ロックの自動生成勤務を置き換え、生成結果を一括保存する。"""

    # 生成中に手入力・ロックされた勤務も守るため、保存直前に固定キーを取得する。
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


def persist_generated_shift(
    shift_plan: ShiftPlan, result: ShiftGenerationResult
) -> None:
    """勤務結果を保存し、シフト表を生成済みに更新する。"""

    save_generated_shift_results(shift_plan, result)
    shift_plan.status = ShiftPlan.StatusChoices.GENERATED
    shift_plan.save(update_fields=["status", "updated_at"])
