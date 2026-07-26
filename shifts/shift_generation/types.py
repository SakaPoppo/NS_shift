from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from staff.models import StaffMember

from ..models import ShiftResult


GENERATABLE_SHIFT_TYPES = (
    ShiftResult.ShiftTypeChoices.DAY,
    ShiftResult.ShiftTypeChoices.NIGHT,
    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
    ShiftResult.ShiftTypeChoices.OFF,
)


class ShiftGenerationViolationType:
    """画面表示・テスト・目的関数で共有する違反種別。"""

    DAY_SHORTAGE = "day_shortage"
    DAY_EXCESS = "day_excess"
    NIGHT_COUNT_IMBALANCE = "night_count_imbalance"
    MAX_CONSECUTIVE_WORK = "max_consecutive_work"


@dataclass(frozen=True)
class GeneratedShift:
    staff_member_id: int
    date: date
    shift_type: str


@dataclass(frozen=True)
class ShiftGenerationViolation:
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
    actual_day_count_vars: dict[date, object] = field(default_factory=dict)
    day_shortage_vars: dict[date, object] = field(default_factory=dict)
    day_excess_vars: dict[date, object] = field(default_factory=dict)
    total_day_shortage: object | None = None
    total_day_excess: object | None = None
    max_day_shortage: object | None = None


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
class ShiftOptimizationOutput:
    """最適化層から結果組み立て層へ渡す、求解済みデータ。"""

    solver: object
    solver_status: str
    shift_vars: dict
    staffing_data: StaffingObjectiveData
    safety_data: SafetyObjectiveData
    staffing_balance_data: StaffingBalanceData
    ability_balance_data: AbilityBalanceData
    long_streak_terms: list
    phase_results: list[OptimizationPhaseResult]


@dataclass(frozen=True)
class GenerationContext:
    shift_rule: object
    month_dates: list[date]
    staff_members: list[StaffMember]
    fixed_assignments: dict[tuple[int, date], str]
    effective_rules: dict[date, object]
    previous_consecutive_work_days: dict[int, int]
    effective_off_days: dict[int, int]


class ShiftGenerationError(Exception):
    """固定条件の矛盾やソルバー不成立を呼び出し元へ伝える例外。"""
