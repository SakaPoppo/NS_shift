from __future__ import annotations

from django.db.models import Q

from staff.models import StaffMember

from ..models import DayOffRequest, ShiftCarryover, ShiftResult
from ..services import (
    OFF_LIKE_SHIFT_TYPES,
    get_effective_rule_for_date,
    get_japanese_holiday_dates,
    get_month_dates,
)
from .types import (
    GenerationContext,
    GenerationIssue,
    GenerationIssueCode,
    GenerationIssueSeverity,
    ShiftGenerationError,
)


def load_generation_context(shift_plan) -> GenerationContext:
    """生成データを読み込み、固定条件だけで判定できる矛盾を検出する。"""

    shift_rule = getattr(shift_plan, "shift_rule", None)
    if shift_rule is None:
        raise ShiftGenerationError(
            issue=GenerationIssue(
                code=GenerationIssueCode.SHIFT_RULE_NOT_CONFIGURED,
                severity=GenerationIssueSeverity.ERROR,
            )
        )

    month_dates = get_month_dates(shift_plan.year, shift_plan.month)
    holiday_dates = get_japanese_holiday_dates(shift_plan.year, shift_plan.month)
    all_staff_members = list(
        StaffMember.objects.filter(
            user=shift_plan.user,
            is_active=True,
        )
        .prefetch_related("regular_days_off")
        .order_by("id")
    )
    if not all_staff_members:
        raise ShiftGenerationError(
            issue=GenerationIssue(
                code=GenerationIssueCode.NO_ACTIVE_STAFF,
                severity=GenerationIssueSeverity.ERROR,
            )
        )

    excluded_staff_ids = shift_plan.get_excluded_staff_ids()
    staff_members = [
        staff_member
        for staff_member in all_staff_members
        if staff_member.id not in excluded_staff_ids
    ]
    if not staff_members:
        raise ShiftGenerationError(
            issue=GenerationIssue(
                code=GenerationIssueCode.NO_GENERATION_TARGET_STAFF,
                severity=GenerationIssueSeverity.ERROR,
            )
        )

    weekday_rules = list(shift_plan.weekday_rules.all())
    date_rules = list(shift_plan.date_rules.all())
    shift_plan._weekday_rule_map = {
        rule.day_of_week: rule for rule in weekday_rules
    }
    shift_plan._date_rule_map = {rule.target_date: rule for rule in date_rules}

    day_off_requests = {
        (request.staff_member_id, request.date): request
        for request in DayOffRequest.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        )
    }
    fixed_results = {
        (result.staff_member_id, result.date): result
        for result in ShiftResult.objects.filter(
            shift_plan=shift_plan,
            staff_member__in=staff_members,
        ).filter(
            Q(input_type=ShiftResult.InputTypeChoices.MANUAL) | Q(is_locked=True)
        )
    }
    user_override_assignment_keys = {
        cell_key
        for cell_key, result in fixed_results.items()
        if (
            result.input_type == ShiftResult.InputTypeChoices.MANUAL
            or result.lock_reason == ShiftResult.LockReasonChoices.USER
        )
    }
    regular_day_offs = {
        staff.id: {
            day_off.day_of_week for day_off in staff.regular_days_off.all()
        }
        for staff in staff_members
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
    night_capable_count = sum(staff.can_night_shift for staff in staff_members)
    for target_date, effective_rule in effective_rules.items():
        if effective_rule.required_night_staff > night_capable_count:
            raise ShiftGenerationError(
                issue=GenerationIssue(
                    code=GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
                    severity=GenerationIssueSeverity.ERROR,
                    dates=[target_date],
                    details={
                        "required_count": effective_rule.required_night_staff,
                        "available_count": night_capable_count,
                    },
                )
            )

    previous_consecutive_work_days = {
        carryover.staff_member_id: carryover.previous_consecutive_work_days
        for carryover in shift_plan.carryovers.filter(
            staff_member__in=staff_members,
            source__in=(
                ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
                ShiftCarryover.SourceChoices.MANUAL,
            ),
        )
    }
    mandatory_off_counts = {
        staff.id: len(
            {
                target_date
                for target_date in month_dates
                if target_date.weekday()
                in regular_day_offs.get(staff.id, set())
                or (staff.is_holiday_off and target_date in holiday_dates)
                or (
                    fixed_results.get((staff.id, target_date)) is not None
                    and fixed_results[(staff.id, target_date)].lock_reason
                    == ShiftResult.LockReasonChoices.MONTH_BOUNDARY
                    and fixed_results[(staff.id, target_date)].shift_type
                    == ShiftResult.ShiftTypeChoices.OFF
                )
            }
        )
        for staff in staff_members
    }
    effective_off_days = {
        staff_id: max(shift_rule.off_days_per_staff, count)
        for staff_id, count in mandatory_off_counts.items()
    }
    for staff in staff_members:
        requested_off_dates = [
            target_date
            for target_date in month_dates
            if (staff.id, target_date) in day_off_requests
        ]
        if len(requested_off_dates) > effective_off_days[staff.id]:
            raise ShiftGenerationError(
                issue=GenerationIssue(
                    code=GenerationIssueCode.TOO_MANY_DAY_OFF_REQUESTS,
                    severity=GenerationIssueSeverity.ERROR,
                    dates=requested_off_dates,
                    staff_ids=[staff.id],
                    details={
                        "staff_name": staff.name,
                        "monthly_off_days": effective_off_days[staff.id],
                        "requested_off_count": len(requested_off_dates),
                    },
                )
            )

    _validate_fixed_assignments(
        staff_members=staff_members,
        month_dates=month_dates,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
        user_override_assignment_keys=user_override_assignment_keys,
    )
    return GenerationContext(
        shift_rule=shift_rule,
        month_dates=month_dates,
        staff_members=staff_members,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
        previous_consecutive_work_days=previous_consecutive_work_days,
        effective_off_days=effective_off_days,
        user_override_assignment_keys=user_override_assignment_keys,
    )


def _build_fixed_assignments(
    *,
    month_dates,
    staff_members,
    day_off_requests,
    fixed_results,
    regular_day_offs,
    holiday_dates,
):
    fixed_assignments = {}
    for staff in staff_members:
        regular_days = regular_day_offs.get(staff.id, set())
        for target_date in month_dates:
            cell_key = (staff.id, target_date)
            if cell_key in day_off_requests:
                fixed_assignments[cell_key] = (
                    ShiftResult.ShiftTypeChoices.OFF_REQUEST
                )
            elif (
                target_date.weekday() in regular_days
                or (staff.is_holiday_off and target_date in holiday_dates)
            ):
                fixed_assignments[cell_key] = ShiftResult.ShiftTypeChoices.OFF

    for cell_key, shift_result in fixed_results.items():
        existing_shift_type = fixed_assignments.get(cell_key)
        if (
            existing_shift_type is not None
            and existing_shift_type != shift_result.shift_type
        ):
            staff_member_id, target_date = cell_key
            staff_member = next(
                staff
                for staff in staff_members
                if staff.id == staff_member_id
            )
            raise ShiftGenerationError(
                issue=GenerationIssue(
                    code=GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT,
                    severity=GenerationIssueSeverity.ERROR,
                    dates=[target_date],
                    staff_ids=[staff_member_id],
                    details={
                        "staff_name": staff_member.name,
                        "fixed_shift_type": existing_shift_type,
                        "saved_shift_type": shift_result.shift_type,
                    },
                )
            )
        fixed_assignments[cell_key] = shift_result.shift_type
    return fixed_assignments


def _validate_fixed_assignments(
    *,
    staff_members,
    month_dates,
    fixed_assignments,
    effective_rules,
    user_override_assignment_keys=frozenset(),
):
    for staff in staff_members:
        for index, target_date in enumerate(month_dates):
            fixed_shift_type = fixed_assignments.get((staff.id, target_date))
            if fixed_shift_type is None:
                continue
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.NIGHT
                and not staff.can_night_shift
            ):
                raise ShiftGenerationError(
                    issue=GenerationIssue(
                        code=GenerationIssueCode.NIGHT_SHIFT_NOT_ALLOWED,
                        severity=GenerationIssueSeverity.ERROR,
                        dates=[target_date],
                        staff_ids=[staff.id],
                        details={"staff_name": staff.name},
                    )
                )
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                and index > 0
            ):
                previous_shift_type = fixed_assignments.get(
                    (staff.id, month_dates[index - 1])
                )
                if (
                    previous_shift_type is not None
                    and previous_shift_type != ShiftResult.ShiftTypeChoices.NIGHT
                ):
                    raise ShiftGenerationError(
                        issue=_night_sequence_issue(
                            staff,
                            [month_dates[index - 1], target_date],
                        )
                    )
            if (
                fixed_shift_type == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                and index == 0
                and len(month_dates) >= 2
                and not effective_rules[target_date].night_shift_next_day_off
            ):
                next_shift_type = fixed_assignments.get(
                    (staff.id, month_dates[index + 1])
                )
                if next_shift_type is not None and next_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {ShiftResult.ShiftTypeChoices.NIGHT}
                ):
                    raise ShiftGenerationError(
                        issue=_night_sequence_issue(
                            staff,
                            [target_date, month_dates[index + 1]],
                        )
                    )
            if fixed_shift_type != ShiftResult.ShiftTypeChoices.NIGHT:
                continue
            if index + 1 >= len(month_dates):
                continue
            next_shift_type = fixed_assignments.get(
                (staff.id, month_dates[index + 1])
            )
            if (
                next_shift_type is not None
                and next_shift_type != ShiftResult.ShiftTypeChoices.AFTER_NIGHT
            ):
                raise ShiftGenerationError(
                    issue=_night_sequence_issue(
                        staff,
                        [target_date, month_dates[index + 1]],
                    )
                )
            if index + 2 >= len(month_dates):
                continue
            third_key = (staff.id, month_dates[index + 2])
            third_shift_type = fixed_assignments.get(
                third_key
            )
            night_sequence_keys = (
                (staff.id, target_date),
                (staff.id, month_dates[index + 1]),
                third_key,
            )
            is_manual_only_night_sequence = all(
                key in user_override_assignment_keys
                for key in night_sequence_keys
            )
            rule = effective_rules[target_date]
            if rule.night_shift_next_day_off:
                if (
                    third_shift_type is not None
                    and third_shift_type not in OFF_LIKE_SHIFT_TYPES
                    and not is_manual_only_night_sequence
                ):
                    raise ShiftGenerationError(
                        issue=_night_sequence_issue(
                            staff,
                            [target_date, month_dates[index + 1], month_dates[index + 2]],
                        )
                    )
            elif third_shift_type is not None and third_shift_type not in (
                OFF_LIKE_SHIFT_TYPES
                | {ShiftResult.ShiftTypeChoices.NIGHT}
            ):
                raise ShiftGenerationError(
                    issue=_night_sequence_issue(
                        staff,
                        [target_date, month_dates[index + 1], month_dates[index + 2]],
                    )
                )


def _night_sequence_issue(staff, related_dates) -> GenerationIssue:
    return GenerationIssue(
        code=GenerationIssueCode.NIGHT_SEQUENCE_CONFLICT,
        severity=GenerationIssueSeverity.ERROR,
        dates=related_dates,
        staff_ids=[staff.id],
        details={"staff_name": staff.name},
    )
