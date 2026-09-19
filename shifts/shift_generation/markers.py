"""生成Issueをシフト表のマーキング位置へ変換する。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .messages import ISSUE_TITLES
from .types import GenerationIssue, GenerationIssueCode, GenerationIssueSeverity


ISSUE_LEVEL_PRIORITIES = {
    GenerationIssueSeverity.WARNING: 1,
    GenerationIssueSeverity.ERROR: 2,
}


@dataclass
class GenerationIssueMarkers:
    """テンプレートへ渡す4種類のマーキング情報。"""

    date_issue_levels: dict[date, str] = field(default_factory=dict)
    daily_summary_issue_levels: dict[tuple[date, str], str] = field(
        default_factory=dict
    )
    cell_issue_levels: dict[tuple[int, date], str] = field(default_factory=dict)
    staff_summary_issue_levels: dict[tuple[int, str], str] = field(
        default_factory=dict
    )
    date_issue_titles: dict[date, str] = field(default_factory=dict)
    daily_summary_issue_titles: dict[tuple[date, str], str] = field(
        default_factory=dict
    )
    cell_issue_titles: dict[tuple[int, date], str] = field(default_factory=dict)
    staff_summary_issue_titles: dict[tuple[int, str], str] = field(
        default_factory=dict
    )


def build_generation_issue_markers(
    issues: list[GenerationIssue],
    *,
    off_request_cell_keys: set[tuple[int, date]] = frozenset(),
) -> GenerationIssueMarkers:
    """warning/error Issueだけを、表示場所ごとの強いレベルへ集約する。"""

    markers = GenerationIssueMarkers()
    for issue in issues:
        if issue.severity not in ISSUE_LEVEL_PRIORITIES:
            continue

        title = ISSUE_TITLES.get(issue.code, "シフト生成の問題")
        if issue.code in {
            GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
            GenerationIssueCode.DAY_STAFFING_IMBALANCE,
            GenerationIssueCode.INSUFFICIENT_LEADER_STAFF,
        }:
            for target_date in issue.dates:
                _set_marker(
                    markers.date_issue_levels,
                    markers.date_issue_titles,
                    target_date,
                    issue.severity,
                    title,
                )
                _set_marker(
                    markers.daily_summary_issue_levels,
                    markers.daily_summary_issue_titles,
                    (target_date, "day"),
                    issue.severity,
                    title,
                )
        elif issue.code == GenerationIssueCode.NIGHT_COUNT_IMBALANCE:
            for staff_id in issue.staff_ids:
                _set_marker(
                    markers.staff_summary_issue_levels,
                    markers.staff_summary_issue_titles,
                    (staff_id, "night"),
                    issue.severity,
                    title,
                )
        elif issue.code == GenerationIssueCode.MONTHLY_OFF_COUNT_EXCEEDED:
            for staff_id in issue.staff_ids:
                _set_marker(
                    markers.staff_summary_issue_levels,
                    markers.staff_summary_issue_titles,
                    (staff_id, "off"),
                    issue.severity,
                    title,
                )
        elif issue.code == GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF:
            for target_date in issue.dates:
                _set_marker(
                    markers.date_issue_levels,
                    markers.date_issue_titles,
                    target_date,
                    issue.severity,
                    title,
                )
                _set_marker(
                    markers.daily_summary_issue_levels,
                    markers.daily_summary_issue_titles,
                    (target_date, "night"),
                    issue.severity,
                    title,
                )
        elif issue.code == GenerationIssueCode.TOO_MANY_DAY_OFF_REQUESTS:
            for cell_key in off_request_cell_keys:
                if cell_key[0] in issue.staff_ids:
                    _set_marker(
                        markers.cell_issue_levels,
                        markers.cell_issue_titles,
                        cell_key,
                        issue.severity,
                        title,
                    )
        elif issue.code in {
            GenerationIssueCode.NIGHT_SHIFT_NOT_ALLOWED,
            GenerationIssueCode.NIGHT_SEQUENCE_CONFLICT,
            GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT,
        }:
            _mark_issue_cells(markers, issue, title)
        elif issue.code == GenerationIssueCode.GENERATION_INFEASIBLE:
            if issue.dates and issue.staff_ids:
                _mark_issue_cells(markers, issue, title)
            elif issue.dates:
                for target_date in issue.dates:
                    _set_marker(
                        markers.date_issue_levels,
                        markers.date_issue_titles,
                        target_date,
                        issue.severity,
                        title,
                    )
    return markers


def _mark_issue_cells(
    markers: GenerationIssueMarkers,
    issue: GenerationIssue,
    title: str,
) -> None:
    for staff_id in issue.staff_ids:
        for target_date in issue.dates:
            _set_marker(
                markers.cell_issue_levels,
                markers.cell_issue_titles,
                (staff_id, target_date),
                issue.severity,
                title,
            )


def _set_marker(levels, titles, key, level: str, title: str) -> None:
    existing_level = levels.get(key)
    if (
        existing_level is None
        or ISSUE_LEVEL_PRIORITIES[level] > ISSUE_LEVEL_PRIORITIES[existing_level]
    ):
        levels[key] = level
        titles[key] = title
