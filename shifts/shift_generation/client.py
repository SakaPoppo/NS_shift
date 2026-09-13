"""Cloud Run上のNs Shift Optimizer APIを呼び出すクライアント。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import requests
from django.conf import settings

from ..models import ShiftResult
from ..services import WORKLIKE_SHIFT_TYPES
from .payload import build_optimizer_payload
from .results import (
    _build_generation_violations,
    build_day_staffing_adjustment_message,
    build_optimization_incomplete_message,
)
from .types import (
    GeneratedShift,
    GenerationContext,
    ShiftGenerationError,
    ShiftGenerationResult,
    ShiftOptimizationSummary,
)


class OptimizerAPIError(ShiftGenerationError):
    """外部最適化APIの設定・通信・応答の失敗を画面向けに伝える。"""


def generate_with_optimizer_api(
    context: GenerationContext,
) -> ShiftGenerationResult:
    """解決済みのDjangoコンテキストをAPIへ送り、生成結果へ変換する。"""

    endpoint = _get_generate_endpoint()
    api_key = settings.OPTIMIZER_API_KEY
    if not api_key:
        raise OptimizerAPIError("シフト最適化サービスの認証設定が不足しています。")

    try:
        response = requests.post(
            endpoint,
            json=build_optimizer_payload(context),
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            timeout=settings.OPTIMIZER_API_TIMEOUT,
        )
    except requests.exceptions.Timeout as error:
        raise OptimizerAPIError(
            "シフト最適化サービスの応答がタイムアウトしました。時間をおいて再度お試しください。"
        ) from error
    except requests.exceptions.ConnectionError as error:
        raise OptimizerAPIError(
            "シフト最適化サービスへ接続できませんでした。時間をおいて再度お試しください。"
        ) from error
    except requests.exceptions.RequestException as error:
        raise OptimizerAPIError(
            "シフト最適化サービスとの通信に失敗しました。時間をおいて再度お試しください。"
        ) from error

    _raise_for_unsuccessful_status(response)
    try:
        response_data = response.json()
    except ValueError as error:
        raise OptimizerAPIError(
            "シフト最適化サービスから不正な応答が返されました。時間をおいて再度お試しください。"
        ) from error

    return _build_generation_result(context, response_data)


def _get_generate_endpoint() -> str:
    base_url = settings.OPTIMIZER_API_URL
    allowed_schemes = (
        ("https://", "http://") if settings.DEBUG else ("https://",)
    )
    if not base_url.startswith(allowed_schemes):
        raise OptimizerAPIError("シフト最適化サービスの接続先設定が不正です。")
    if settings.OPTIMIZER_API_TIMEOUT <= 0:
        raise OptimizerAPIError("シフト最適化サービスのタイムアウト設定が不正です。")
    return f"{base_url}/generate"


def _raise_for_unsuccessful_status(response: requests.Response) -> None:
    if response.status_code in {401, 403}:
        raise OptimizerAPIError("シフト最適化サービスの認証に失敗しました。")
    if 400 <= response.status_code < 500:
        raise OptimizerAPIError(
            "シフト最適化サービスがリクエストを受け付けませんでした。条件を確認してください。"
        )
    if response.status_code >= 500:
        raise OptimizerAPIError(
            "シフト最適化サービスでエラーが発生しました。時間をおいて再度お試しください。"
        )


def _build_generation_result(
    context: GenerationContext, response_data: object
) -> ShiftGenerationResult:
    if not isinstance(response_data, Mapping):
        raise _invalid_response_error()

    status = response_data.get("status")
    solver_status = response_data.get("solver_status")
    if status != "success" or not isinstance(solver_status, str):
        raise _invalid_response_error()

    shifts = _parse_shifts(context, response_data.get("shifts"))
    phase_statuses, phase_optimal_flags = _parse_phase_results(
        response_data.get("phase_results")
    )
    optimization_summary = _build_summary(
        context=context,
        shifts=shifts,
        phase_statuses=phase_statuses,
        phase_optimal_flags=phase_optimal_flags,
    )
    return ShiftGenerationResult(
        status=status,
        shifts=shifts,
        violations=_build_generation_violations(
            optimization_summary=optimization_summary
        ),
        solver_status=solver_status,
        staff_count=len(context.staff_members),
        target_day_count=len(context.month_dates),
        optimization_summary=optimization_summary,
        day_staffing_adjustment_message=(
            build_day_staffing_adjustment_message(
                optimization_summary=optimization_summary,
                required_day_counts=optimization_summary.required_day_counts.values(),
            )
        ),
        optimization_incomplete_message=(
            build_optimization_incomplete_message(
                optimization_summary=optimization_summary
            )
        ),
    )


def _parse_shifts(
    context: GenerationContext, raw_shifts: object
) -> list[GeneratedShift]:
    if not isinstance(raw_shifts, list):
        raise _invalid_response_error()

    allowed_shift_types = set(ShiftResult.ShiftTypeChoices.values)
    expected_keys = {
        (staff_member.id, target_date)
        for staff_member in context.staff_members
        for target_date in context.month_dates
    }
    parsed_by_key: dict[tuple[int, date], GeneratedShift] = {}
    for item in raw_shifts:
        if not isinstance(item, Mapping):
            raise _invalid_response_error()
        staff_id = item.get("staff_id")
        shift_type = item.get("shift_type")
        raw_date = item.get("date")
        if not isinstance(staff_id, int) or not isinstance(shift_type, str):
            raise _invalid_response_error()
        try:
            target_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError) as error:
            raise _invalid_response_error() from error
        key = (staff_id, target_date)
        if key not in expected_keys or key in parsed_by_key or shift_type not in allowed_shift_types:
            raise _invalid_response_error()
        parsed_by_key[key] = GeneratedShift(
            staff_member_id=staff_id,
            date=target_date,
            shift_type=shift_type,
        )

    if set(parsed_by_key) != expected_keys:
        raise _invalid_response_error()
    return [parsed_by_key[key] for key in sorted(parsed_by_key)]


def _parse_phase_results(raw_phase_results: object) -> tuple[dict[str, str], dict[str, bool]]:
    if not isinstance(raw_phase_results, list):
        raise _invalid_response_error()
    statuses: dict[str, str] = {}
    optimal_flags: dict[str, bool] = {}
    for phase in raw_phase_results:
        if not isinstance(phase, Mapping):
            raise _invalid_response_error()
        name = phase.get("name")
        status = phase.get("status")
        optimal = phase.get("optimal")
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or not isinstance(optimal, bool)
            or name in statuses
        ):
            raise _invalid_response_error()
        statuses[name] = status
        optimal_flags[name] = optimal
    return statuses, optimal_flags


def _build_summary(*, context, shifts, phase_statuses, phase_optimal_flags):
    shift_type_by_key = {
        (shift.staff_member_id, shift.date): shift.shift_type for shift in shifts
    }
    actual_day_counts = {
        target_date: sum(
            shift_type_by_key[(staff_member.id, target_date)]
            == ShiftResult.ShiftTypeChoices.DAY
            for staff_member in context.staff_members
        )
        for target_date in context.month_dates
    }
    required_day_counts = {
        target_date: context.effective_rules[target_date].required_day_staff
        for target_date in context.month_dates
    }
    day_staffing_deltas = {
        target_date: actual_day_counts[target_date] - required_day_counts[target_date]
        for target_date in context.month_dates
    }
    night_shift_counts = {
        staff_member.id: sum(
            shift_type_by_key[(staff_member.id, target_date)]
            == ShiftResult.ShiftTypeChoices.NIGHT
            for target_date in context.month_dates
        )
        for staff_member in context.staff_members
        if staff_member.can_night_shift
    }
    night_count_values = list(night_shift_counts.values())
    return ShiftOptimizationSummary(
        total_actual_day_count=sum(actual_day_counts.values()),
        total_required_day_count=sum(required_day_counts.values()),
        minimum_day_staffing_delta=min(day_staffing_deltas.values(), default=0),
        maximum_day_staffing_delta=max(day_staffing_deltas.values(), default=0),
        day_staffing_delta_range=(
            max(day_staffing_deltas.values(), default=0)
            - min(day_staffing_deltas.values(), default=0)
        ),
        minimum_actual_day_count=min(actual_day_counts.values(), default=0),
        maximum_actual_day_count=max(actual_day_counts.values(), default=0),
        actual_day_counts=actual_day_counts,
        required_day_counts=required_day_counts,
        day_staffing_deltas=day_staffing_deltas,
        night_shift_count_min=min(night_count_values, default=None),
        night_shift_count_max=max(night_count_values, default=None),
        night_count_imbalance_violation=max(
            max(night_count_values, default=0) - min(night_count_values, default=0) - 1,
            0,
        ),
        long_streak_penalty=_calculate_long_streak_penalty(
            context, shift_type_by_key
        ),
        phase_statuses=phase_statuses,
        phase_optimal_flags=phase_optimal_flags,
        night_shift_counts=night_shift_counts,
    )


def _calculate_long_streak_penalty(context, shift_type_by_key) -> int:
    max_consecutive_work_days = context.month_dates and (
        context.effective_rules[context.month_dates[0]].max_consecutive_work_days
    )
    if not max_consecutive_work_days:
        return 0
    thresholds = [(max_consecutive_work_days, 3)]
    if max_consecutive_work_days >= 2:
        thresholds.insert(0, (max_consecutive_work_days - 1, 1))

    penalty = 0
    for staff_member in context.staff_members:
        prefix_count = min(
            context.previous_consecutive_work_days.get(staff_member.id, 0),
            max_consecutive_work_days,
        )
        work_days = [True] * prefix_count + [
            shift_type_by_key[(staff_member.id, target_date)] in WORKLIKE_SHIFT_TYPES
            for target_date in context.month_dates
        ]
        for length, weight in thresholds:
            penalty += weight * sum(
                all(work_days[start_index : start_index + length])
                for start_index in range(len(work_days) - length + 1)
            )
    return penalty


def _invalid_response_error() -> OptimizerAPIError:
    return OptimizerAPIError(
        "シフト最適化サービスから不正な応答が返されました。時間をおいて再度お試しください。"
    )
