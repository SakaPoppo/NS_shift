"""シフト自動生成の窓口。"""

from django.db import transaction

from .models import ShiftPlan
from .shift_generation.client import generate_with_optimizer_api
from .shift_generation.context import load_generation_context
from .shift_generation.persistence import (
    persist_generated_shift,
    save_generated_shift_results,
)
from .shift_generation.types import (
    ShiftGenerationError,
    ShiftGenerationResult,
)


def generate_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """シフト表1か月分の勤務を自動生成し、結果をメモリ上で返す。"""

    return generate_with_optimizer_api(load_generation_context(shift_plan))


def generate_and_save_shift(shift_plan: ShiftPlan) -> ShiftGenerationResult:
    """生成結果を ShiftResult へ保存し、ShiftPlan.status を GENERATED へ更新する。"""

    with transaction.atomic():
        result = generate_shift(shift_plan)
        persist_generated_shift(shift_plan, result)
        return result
