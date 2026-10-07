"""お試し版のSession状態を、安全なプリミティブ値だけで扱う。"""

from __future__ import annotations

from datetime import date

from shifts.models import ShiftResult
from shifts.shift_generation.types import GenerationIssue

TRIAL_SESSION_KEY = "shift_trial"


def assignment_key(staff_id: int, target_date: date) -> str:
    return f"{staff_id}|{target_date.isoformat()}"


def load_trial_state(session, context) -> dict:
    """対象月・スタッフに一致するSession状態だけを復元する。"""

    raw_state = session.get(TRIAL_SESSION_KEY, {})
    first_date = context.month_dates[0]
    if not isinstance(raw_state, dict) or (
        raw_state.get("year"), raw_state.get("month")
    ) != (first_date.year, first_date.month):
        return {
            "saved_assignments": {},
            "generated_assignments": {},
            "generation_issues": [],
        }

    return {
        "saved_assignments": deserialize_assignments(
            raw_state.get("saved_assignments"), context
        ),
        "generated_assignments": deserialize_assignments(
            raw_state.get("generated_assignments"), context
        ),
        "generation_issues": deserialize_issues(raw_state.get("generation_issues")),
    }


def save_trial_state(session, context, state: dict) -> None:
    """SessionへJSON serializableなTrial状態だけを保存する。"""

    first_date = context.month_dates[0]
    session[TRIAL_SESSION_KEY] = {
        "year": first_date.year,
        "month": first_date.month,
        "saved_assignments": serialize_assignments(state["saved_assignments"]),
        "generated_assignments": serialize_assignments(
            state["generated_assignments"]
        ),
        "generation_issues": serialize_issues(state["generation_issues"]),
    }


def clear_trial_state(session) -> None:
    session.pop(TRIAL_SESSION_KEY, None)


def serialize_assignments(assignments: dict[tuple[int, date], str]) -> dict[str, str]:
    return {
        assignment_key(staff_id, target_date): shift_type
        for (staff_id, target_date), shift_type in assignments.items()
        if shift_type
    }


def deserialize_assignments(raw_assignments, context) -> dict[tuple[int, date], str]:
    if not isinstance(raw_assignments, dict):
        return {}

    allowed_staff_ids = {staff.id for staff in context.staff_members}
    allowed_dates = set(context.month_dates)
    allowed_shift_types = set(ShiftResult.ShiftTypeChoices.values)
    assignments = {}
    for raw_key, shift_type in raw_assignments.items():
        if not isinstance(raw_key, str) or not isinstance(shift_type, str):
            continue
        try:
            raw_staff_id, raw_date = raw_key.split("|", 1)
            staff_id = int(raw_staff_id)
            target_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            continue
        if (
            staff_id in allowed_staff_ids
            and target_date in allowed_dates
            and shift_type in allowed_shift_types
        ):
            assignments[(staff_id, target_date)] = shift_type
    return assignments


def serialize_issues(issues: list[GenerationIssue]) -> list[dict]:
    return [
        {
            "code": issue.code,
            "severity": issue.severity,
            "dates": [target_date.isoformat() for target_date in issue.dates],
            "staff_ids": list(issue.staff_ids),
            "details": _serialize_value(issue.details),
        }
        for issue in issues
    ]


def deserialize_issues(raw_issues) -> list[GenerationIssue]:
    if not isinstance(raw_issues, list):
        return []

    issues = []
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, dict):
            continue
        try:
            dates = [date.fromisoformat(value) for value in raw_issue["dates"]]
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not isinstance(raw_issue.get("code"), str)
            or not isinstance(raw_issue.get("severity"), str)
            or not isinstance(raw_issue.get("staff_ids"), list)
            or not all(isinstance(staff_id, int) for staff_id in raw_issue["staff_ids"])
            or not isinstance(raw_issue.get("details"), dict)
        ):
            continue
        issues.append(
            GenerationIssue(
                code=raw_issue["code"],
                severity=raw_issue["severity"],
                dates=dates,
                staff_ids=raw_issue["staff_ids"],
                details=raw_issue["details"],
            )
        )
    return issues


def _serialize_value(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
