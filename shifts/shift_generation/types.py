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


class GenerationIssueSeverity:
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class GenerationIssueCode:
    SHIFT_GENERATED = "SHIFT_GENERATED"
    DAY_STAFFING_ABOVE_REQUIRED = "DAY_STAFFING_ABOVE_REQUIRED"
    DAY_STAFFING_BELOW_REQUIRED = "DAY_STAFFING_BELOW_REQUIRED"
    DAY_STAFFING_IMBALANCE = "DAY_STAFFING_IMBALANCE"
    NIGHT_COUNT_IMBALANCE = "NIGHT_COUNT_IMBALANCE"
    DAY_ABILITY_BELOW_TARGET = "DAY_ABILITY_BELOW_TARGET"
    DAY_ABILITY_ABOVE_TARGET = "DAY_ABILITY_ABOVE_TARGET"
    NIGHT_ABILITY_BELOW_TARGET = "NIGHT_ABILITY_BELOW_TARGET"
    NIGHT_ABILITY_ABOVE_TARGET = "NIGHT_ABILITY_ABOVE_TARGET"
    MONTHLY_OFF_COUNT_EXCEEDED = "MONTHLY_OFF_COUNT_EXCEEDED"
    OPTIMIZATION_INCOMPLETE = "OPTIMIZATION_INCOMPLETE"
    SHIFT_RULE_NOT_CONFIGURED = "SHIFT_RULE_NOT_CONFIGURED"
    NO_ACTIVE_STAFF = "NO_ACTIVE_STAFF"
    NO_GENERATION_TARGET_STAFF = "NO_GENERATION_TARGET_STAFF"
    INSUFFICIENT_NIGHT_STAFF = "INSUFFICIENT_NIGHT_STAFF"
    INSUFFICIENT_LEADER_STAFF = "INSUFFICIENT_LEADER_STAFF"
    TOO_MANY_DAY_OFF_REQUESTS = "TOO_MANY_DAY_OFF_REQUESTS"
    NIGHT_SHIFT_NOT_ALLOWED = "NIGHT_SHIFT_NOT_ALLOWED"
    NIGHT_SEQUENCE_CONFLICT = "NIGHT_SEQUENCE_CONFLICT"
    FIXED_ASSIGNMENT_CONFLICT = "FIXED_ASSIGNMENT_CONFLICT"
    GENERATION_INFEASIBLE = "GENERATION_INFEASIBLE"
    OPTIMIZER_API_ERROR = "OPTIMIZER_API_ERROR"


@dataclass(frozen=True)
class GenerationIssue:
    """生成処理で発生した事実を、表示文言から分離して保持する。"""

    code: str
    severity: str
    dates: list[date] = field(default_factory=list)
    staff_ids: list[int] = field(default_factory=list)
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GeneratedShift:
    staff_member_id: int
    date: date
    shift_type: str


@dataclass(frozen=True)
class ShiftOptimizationSummary:
    total_actual_day_count: int
    total_required_day_count: int
    minimum_day_staffing_delta: int
    maximum_day_staffing_delta: int
    day_staffing_delta_range: int
    minimum_actual_day_count: int
    maximum_actual_day_count: int
    actual_day_counts: dict[date, int]
    required_day_counts: dict[date, int]
    day_staffing_deltas: dict[date, int]
    night_shift_count_min: int | None
    night_shift_count_max: int | None
    night_count_imbalance_violation: int
    long_streak_penalty: int
    phase_statuses: dict[str, str] = field(default_factory=dict)
    phase_optimal_flags: dict[str, bool] = field(default_factory=dict)
    non_optimal_phases: tuple[str, ...] = ()
    night_shift_counts: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ShiftGenerationResult:
    status: str
    shifts: list[GeneratedShift]
    issues: list[GenerationIssue] = field(default_factory=list)
    solver_status: str | None = None
    staff_count: int = 0
    target_day_count: int = 0
    optimization_summary: ShiftOptimizationSummary | None = None


@dataclass(frozen=True)
class GenerationContext:
    shift_rule: object
    month_dates: list[date]
    staff_members: list[StaffMember]
    fixed_assignments: dict[tuple[int, date], str]
    effective_rules: dict[date, object]
    previous_consecutive_work_days: dict[int, int]
    effective_off_days: dict[int, int]
    user_override_assignment_keys: set[tuple[int, date]] = field(
        default_factory=set
    )


class ShiftGenerationError(Exception):
    """固定条件の矛盾やソルバー不成立を呼び出し元へ伝える例外。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        issue: GenerationIssue | None = None,
        issues: list[GenerationIssue] | None = None,
    ):
        default_issue = GenerationIssue(
            code=GenerationIssueCode.GENERATION_INFEASIBLE,
            severity=GenerationIssueSeverity.ERROR,
            details={"reason": message} if message else {},
        )
        self.issues = issues or [issue or default_issue]
        self.issue = self.issues[0]
        super().__init__(message or self.issue.code)
