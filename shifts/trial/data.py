"""お試し版で利用するDB非依存の固定サンプルデータ。"""

from dataclasses import dataclass

from shifts.shift_generation.types import GenerationStaff


@dataclass(frozen=True)
class TrialShiftRule:
    """お試し版の全日共通の勤務条件。"""

    required_day_staff: int = 7
    required_night_staff: int = 3
    required_leader_staff: int = 1
    off_days_per_staff: int = 10
    max_consecutive_work_days: int = 5
    night_shift_next_day_off: bool = True


TRIAL_STAFF_MEMBERS = (
    GenerationStaff(1, "看護師01", "leader", 5, True),
    GenerationStaff(2, "看護師02", "leader", 5, True),
    GenerationStaff(3, "看護師03", "leader", 4, True),
    GenerationStaff(4, "看護師04", "leader", 4, True),
    GenerationStaff(5, "看護師05", "leader", 4, True),
    GenerationStaff(6, "看護師06", "leader", 4, True),
    GenerationStaff(7, "看護師07", "leader", 3, True),
    GenerationStaff(8, "看護師08", "leader", 3, True),
    GenerationStaff(9, "看護師09", "leader", 3, True),
    GenerationStaff(10, "看護師10", "member", 3, True),
    GenerationStaff(11, "看護師11", "member", 3, True),
    GenerationStaff(12, "看護師12", "member", 2, True),
    GenerationStaff(13, "看護師13", "member", 2, True),
    GenerationStaff(14, "看護師14", "member", 2, True),
    GenerationStaff(15, "看護師15", "member", 2, True),
    GenerationStaff(16, "看護師16", "member", 2, True),
    GenerationStaff(17, "看護師17", "member", 1, True),
    GenerationStaff(18, "看護師18", "member", 1, True, (2,)),
    GenerationStaff(19, "看護師19", "member", 1, True, (5,)),
    GenerationStaff(20, "看護師20", "member", 1, True, (6,)),
)

TRIAL_PREVIOUS_NIGHT_STAFF_IDS = (2, 9, 15)
TRIAL_PREVIOUS_AFTER_NIGHT_STAFF_IDS = (4, 11, 17)
