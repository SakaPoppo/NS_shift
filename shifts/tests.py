import csv
import json
import secrets
from datetime import date
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from config.settings import resolve_optimizer_api_url
from staff.models import StaffMember, StaffRegularDayOff

from .shift_generation.context import load_generation_context
from .shift_generation.client import (
    OptimizerAPIError,
    generate_with_optimizer_api,
)
from .shift_generation.persistence import save_generated_shift_results
from .forms import ShiftCarryoverEntryForm, ShiftPlanCreateForm, ShiftRuleForm
from .shift_generator import (
    ShiftGenerationError,
    ShiftGenerationResult,
    generate_and_save_shift,
    generate_shift,
)
from .shift_generation.payload import build_optimizer_payload
from .shift_generation.messages import format_generation_issue
from .shift_generation.markers import build_generation_issue_markers
from .shift_generation.types import (
    GenerationContext,
    GenerationIssue,
    GenerationIssueCode,
    GenerationIssueSeverity,
)
from .models import DateShiftRule, DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule
from .services import (
    EffectiveShiftRule,
    WORKLIKE_SHIFT_TYPES,
    build_shift_carryovers,
    calculate_previous_consecutive_work_days,
    get_effective_rule_for_date,
    get_japanese_holiday_dates,
    get_month_dates,
    get_previous_plan_carryover_values,
    get_usable_previous_shift_plan,
    save_manual_shift_carryovers,
    sync_month_boundary_assignments,
)
from .views import (
    ShiftPlanCsvExportView,
    ShiftRuleEditView,
    build_day_headers,
    build_shift_plan_grid,
)


class OptimizerApiSettingsTests(SimpleTestCase):
    def test_debug_uses_local_optimizer_api_url(self):
        with patch.dict(
            "os.environ",
            {
                "LOCAL_OPTIMIZER_API_URL": "http://optimizer.local:8080/",
                "OPTIMIZER_API_URL": "https://optimizer.example.run.app",
            },
            clear=False,
        ):
            self.assertEqual(
                resolve_optimizer_api_url(debug=True),
                "http://optimizer.local:8080",
            )

    def test_non_debug_uses_cloud_run_optimizer_api_url(self):
        with patch.dict(
            "os.environ",
            {
                "LOCAL_OPTIMIZER_API_URL": "http://optimizer.local:8080",
                "OPTIMIZER_API_URL": "https://optimizer.example.run.app/",
            },
            clear=False,
        ):
            self.assertEqual(
                resolve_optimizer_api_url(debug=False),
                "https://optimizer.example.run.app",
            )


class ShiftPlanModelTests(TestCase):
    def test_display_title_is_generated_from_year_and_month(self):
        user = get_user_model().objects.create_user(
            username="plan-user",
            password="password123",
        )
        shift_plan = ShiftPlan.objects.create(
            user=user,
            year=2026,
            month=7,
        )

        self.assertEqual(shift_plan.display_title, "2026年7月 シフト表")
        self.assertEqual(str(shift_plan), "2026年7月 シフト表")


class ShiftGenerationContextTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="generation-context-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=7,
        )
        ShiftRule.objects.create(
            shift_plan=self.shift_plan,
            required_day_staff=0,
            required_night_staff=0,
            required_leader_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.target_staff = StaffMember.objects.create(
            user=self.user,
            name="生成対象",
        )
        self.excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="生成対象外",
        )

    def optimizer_api_success_response(self, context):
        response = Mock(status_code=200)
        response.json.return_value = {
            "status": "success",
            "solver_status": "OPTIMAL",
            "shifts": [
                {
                    "staff_id": staff_member.id,
                    "date": target_date.isoformat(),
                    "shift_type": ShiftResult.ShiftTypeChoices.OFF,
                }
                for staff_member in context.staff_members
                for target_date in context.month_dates
            ],
            "phase_results": [],
            "issues": [
                {
                    "code": GenerationIssueCode.SHIFT_GENERATED,
                    "severity": GenerationIssueSeverity.SUCCESS,
                    "dates": [],
                    "staff_ids": [],
                    "details": {},
                }
            ],
        }
        return response

    def test_context_contains_only_generation_target_staff(self):
        self.shift_plan.excluded_staffs.add(self.excluded_staff)

        context = load_generation_context(self.shift_plan)

        self.assertEqual(
            [staff_member.id for staff_member in context.staff_members],
            [self.target_staff.id],
        )

    def test_context_includes_all_staff_when_none_are_excluded(self):
        context = load_generation_context(self.shift_plan)

        self.assertEqual(
            [staff_member.id for staff_member in context.staff_members],
            [self.target_staff.id, self.excluded_staff.id],
        )

    def test_context_rejects_when_all_staff_are_excluded(self):
        self.shift_plan.excluded_staffs.add(
            self.target_staff,
            self.excluded_staff,
        )

        with self.assertRaises(ShiftGenerationError) as raised:
            load_generation_context(self.shift_plan)

        self.assertEqual(
            raised.exception.issue.code,
            GenerationIssueCode.NO_GENERATION_TARGET_STAFF,
        )

    def test_context_structures_fixed_assignment_conflict(self):
        target_date = date(2026, 7, 1)
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=target_date,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=target_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        with self.assertRaises(ShiftGenerationError) as raised:
            load_generation_context(self.shift_plan)

        issue = raised.exception.issue
        self.assertEqual(
            issue.code,
            GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT,
        )
        self.assertEqual(issue.severity, GenerationIssueSeverity.ERROR)
        self.assertEqual(issue.dates, [target_date])
        self.assertEqual(issue.staff_ids, [self.target_staff.id])
        self.assertEqual(issue.details["fixed_shift_type"], ShiftResult.ShiftTypeChoices.OFF_REQUEST)
        self.assertEqual(issue.details["saved_shift_type"], ShiftResult.ShiftTypeChoices.DAY)
        self.assertEqual(issue.details["staff_name"], self.target_staff.name)
        title, body = format_generation_issue(issue)
        self.assertEqual(title, "勤務条件が競合しています")
        self.assertIn(self.target_staff.name, body)
        self.assertIn("希望休", body)
        self.assertIn("日勤", body)

    def test_context_marks_only_previous_day_and_after_night_for_invalid_after_night(self):
        previous_date = date(2026, 7, 14)
        after_night_date = date(2026, 7, 15)
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=previous_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=after_night_date,
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        with self.assertRaises(ShiftGenerationError) as raised:
            load_generation_context(self.shift_plan)

        self.assertEqual(
            raised.exception.issue.dates,
            [previous_date, after_night_date],
        )

    def test_context_marks_only_night_and_next_day_for_invalid_after_night(self):
        night_date = date(2026, 7, 15)
        next_date = date(2026, 7, 16)
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=night_date,
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=next_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        with self.assertRaises(ShiftGenerationError) as raised:
            load_generation_context(self.shift_plan)

        self.assertEqual(raised.exception.issue.dates, [night_date, next_date])

    def test_context_marks_night_after_night_and_third_day_for_invalid_day_off(self):
        night_date = date(2026, 7, 15)
        after_night_date = date(2026, 7, 16)
        third_date = date(2026, 7, 17)
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=night_date,
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=third_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=True,
        )

        with self.assertRaises(ShiftGenerationError) as raised:
            load_generation_context(self.shift_plan)

        self.assertEqual(
            raised.exception.issue.dates,
            [night_date, after_night_date, third_date],
        )

    def test_context_includes_target_carryovers_regardless_of_source(self):
        manual_carryover_staff = StaffMember.objects.create(
            user=self.user,
            name="手入力月末情報",
        )
        staff_without_carryover = StaffMember.objects.create(
            user=self.user,
            name="月末情報なし",
        )
        self.shift_plan.excluded_staffs.add(self.excluded_staff)
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_consecutive_work_days=3,
        )
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=manual_carryover_staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_consecutive_work_days=4,
        )
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.excluded_staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_consecutive_work_days=2,
        )

        context = load_generation_context(self.shift_plan)

        self.assertEqual(
            context.previous_consecutive_work_days,
            {
                self.target_staff.id: 3,
                manual_carryover_staff.id: 4,
            },
        )
        self.assertNotIn(
            staff_without_carryover.id,
            context.previous_consecutive_work_days,
        )

    def test_context_marks_only_manual_and_user_locked_results_as_user_overrides(self):
        manual_date = date(2026, 7, 1)
        user_locked_date = date(2026, 7, 2)
        boundary_date = date(2026, 7, 3)
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=manual_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=user_locked_date,
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            is_locked=True,
            lock_reason=ShiftResult.LockReasonChoices.USER,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            date=boundary_date,
            shift_type=ShiftResult.ShiftTypeChoices.OFF,
            is_locked=True,
            lock_reason=ShiftResult.LockReasonChoices.MONTH_BOUNDARY,
        )

        context = load_generation_context(self.shift_plan)

        self.assertEqual(
            context.user_override_assignment_keys,
            {
                (self.target_staff.id, manual_date),
                (self.target_staff.id, user_locked_date),
            },
        )

    def test_context_does_not_count_paid_or_special_leave_as_monthly_off_days(self):
        self.shift_plan.shift_rule.off_days_per_staff = 1
        self.shift_plan.shift_rule.save(update_fields=["off_days_per_staff"])
        for target_date, shift_type in (
            (date(2026, 7, 1), ShiftResult.ShiftTypeChoices.PAID_LEAVE),
            (date(2026, 7, 2), ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE),
        ):
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=self.target_staff,
                date=target_date,
                shift_type=shift_type,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        context = load_generation_context(self.shift_plan)

        self.assertEqual(context.effective_off_days[self.target_staff.id], 1)

    def test_context_allows_regular_day_offs_even_when_they_exceed_rule_off_days(self):
        self.shift_plan.shift_rule.off_days_per_staff = 0
        self.shift_plan.shift_rule.save(update_fields=["off_days_per_staff"])
        StaffRegularDayOff.objects.create(
            staff_member=self.target_staff,
            day_of_week=date(2026, 7, 1).weekday(),
        )

        context = load_generation_context(self.shift_plan)

        self.assertGreater(context.effective_off_days[self.target_staff.id], 0)

    def test_build_optimizer_payload_serializes_generation_context(self):
        target_date = date(2026, 9, 1)
        next_date = date(2026, 9, 2)
        staff_member = StaffMember.objects.create(
            user=self.user,
            name="payload対象",
            role=StaffMember.RoleChoices.LEADER,
            ability_level=4,
            can_night_shift=True,
        )
        StaffRegularDayOff.objects.create(
            staff_member=staff_member,
            day_of_week=StaffRegularDayOff.DayOfWeekChoices.MONDAY,
        )
        effective_rule = EffectiveShiftRule(
            required_day_staff=8,
            required_day_staff_override=8,
            required_night_staff=3,
            required_leader_staff=1,
            min_ability_level=4,
            min_ability_level_staff_count=2,
            max_consecutive_work_days=5,
            night_shift_next_day_off=True,
        )
        context = GenerationContext(
            shift_rule=self.shift_plan.shift_rule,
            month_dates=[target_date, next_date],
            staff_members=[staff_member],
            fixed_assignments={(staff_member.id, target_date): "night"},
            effective_rules={
                target_date: effective_rule,
                next_date: effective_rule,
            },
            previous_consecutive_work_days={staff_member.id: 2},
            effective_off_days={staff_member.id: 9},
        )

        payload = build_optimizer_payload(context)

        self.assertEqual(payload["month_dates"], ["2026-09-01", "2026-09-02"])
        self.assertEqual(
            payload["staff_members"],
            [
                {
                    "id": staff_member.id,
                    "role": StaffMember.RoleChoices.LEADER,
                    "ability_level": 4,
                    "can_night_shift": True,
                    "regular_days_off": [0],
                }
            ],
        )
        self.assertEqual(
            payload["fixed_assignments"],
            [
                {
                    "staff_id": staff_member.id,
                    "date": "2026-09-01",
                    "shift_type": "night",
                }
            ],
        )
        self.assertEqual(
            payload["effective_rules"],
            [
                {
                    "date": "2026-09-01",
                    "required_day_staff": 8,
                    "required_day_staff_override": 8,
                    "required_night_staff": 3,
                    "required_leader_staff": 1,
                    "min_ability_level": 4,
                    "min_ability_level_staff_count": 2,
                    "max_consecutive_work_days": 5,
                    "night_shift_next_day_off": True,
                },
                {
                    "date": "2026-09-02",
                    "required_day_staff": 8,
                    "required_day_staff_override": 8,
                    "required_night_staff": 3,
                    "required_leader_staff": 1,
                    "min_ability_level": 4,
                    "min_ability_level_staff_count": 2,
                    "max_consecutive_work_days": 5,
                    "night_shift_next_day_off": True,
                },
            ],
        )
        self.assertEqual(
            payload["previous_consecutive_work_days"],
            [
                {
                    "staff_id": staff_member.id,
                    "previous_consecutive_work_days": 2,
                }
            ],
        )
        self.assertEqual(
            payload["effective_off_days"],
            [{"staff_id": staff_member.id, "off_days": 9}],
        )
        self.assertEqual(
            payload["configured_off_days"],
            [{"staff_id": staff_member.id, "off_days": 0}],
        )
        self.assertEqual(payload["user_override_assignment_keys"], [])
        json.dumps(payload)

    def test_optimizer_payload_includes_manual_carryover_consecutive_work_days(self):
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.target_staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_consecutive_work_days=4,
        )

        payload = build_optimizer_payload(load_generation_context(self.shift_plan))

        self.assertIn(
            {
                "staff_id": self.target_staff.id,
                "previous_consecutive_work_days": 4,
            },
            payload["previous_consecutive_work_days"],
        )

    def test_optimizer_api_client_sends_payload_and_authentication_header(self):
        context = load_generation_context(self.shift_plan)
        api_key = secrets.token_urlsafe(32)
        response = self.optimizer_api_success_response(context)

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app/",
            OPTIMIZER_API_KEY=api_key,
            OPTIMIZER_API_TIMEOUT=330,
        ):
            with patch(
                "shifts.shift_generation.client.requests.post",
                return_value=response,
            ) as mock_post:
                result = generate_with_optimizer_api(context)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.shifts), len(response.json.return_value["shifts"]))
        self.assertIn(
            GenerationIssueCode.SHIFT_GENERATED,
            [issue.code for issue in result.issues],
        )
        mock_post.assert_called_once_with(
            "https://optimizer.example.run.app/generate",
            json=build_optimizer_payload(context),
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            timeout=330,
        )

    def test_optimizer_api_client_exposes_non_optimal_phase_diagnostics(self):
        context = load_generation_context(self.shift_plan)
        response = self.optimizer_api_success_response(context)
        response.json.return_value["phase_results"] = [
            {
                "name": "night_count_balance",
                "status": "OPTIMAL",
                "objective_value": 0,
                "optimal": True,
            },
            {
                "name": "night_ability_balance",
                "status": "FEASIBLE",
                "objective_value": 4,
                "optimal": False,
            },
            {
                "name": "day_staffing_balance",
                "status": "UNKNOWN",
                "objective_value": None,
                "optimal": False,
            },
            {
                "name": "day_ability_balance",
                "status": "NOT_RUN",
                "objective_value": None,
                "optimal": False,
            },
        ]

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app",
            OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
        ), patch(
            "shifts.shift_generation.client.requests.post",
            return_value=response,
        ):
            result = generate_with_optimizer_api(context)

        summary = result.optimization_summary
        self.assertIsNotNone(summary)
        self.assertEqual(summary.phase_statuses["night_count_balance"], "OPTIMAL")
        self.assertTrue(summary.phase_optimal_flags["night_count_balance"])
        self.assertFalse(summary.phase_optimal_flags["night_ability_balance"])
        self.assertEqual(
            summary.non_optimal_phases,
            ("night_ability_balance", "day_staffing_balance", "day_ability_balance"),
        )

    def test_optimizer_api_client_uses_api_issues_for_messages_and_markers(self):
        context = load_generation_context(self.shift_plan)
        response = self.optimizer_api_success_response(context)
        target_date = context.month_dates[0]
        response.json.return_value["issues"] = [
            {
                "code": GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                "severity": GenerationIssueSeverity.WARNING,
                "dates": [target_date.isoformat()],
                "staff_ids": [],
                "details": {
                    "required_count": 5,
                    "actual_count": 3,
                },
            },
            {
                "code": GenerationIssueCode.NIGHT_COUNT_IMBALANCE,
                "severity": GenerationIssueSeverity.WARNING,
                "dates": [],
                "staff_ids": [self.target_staff.id],
                "details": {
                    "minimum_count": 1,
                    "maximum_count": 3,
                    "count_difference": 2,
                },
            },
            {
                "code": GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                "severity": GenerationIssueSeverity.WARNING,
                "dates": [],
                "staff_ids": [],
                "details": {"incomplete_items": ["long_streak"]},
            },
            {
                "code": GenerationIssueCode.DAY_ABILITY_BELOW_TARGET,
                "severity": GenerationIssueSeverity.WARNING,
                "dates": [target_date.isoformat()],
                "staff_ids": [],
                "details": {
                    "date": target_date.isoformat(),
                    "actual_ability_total": 5,
                    "expected_ability_total": 7.0,
                    "deviation_rate": 2 / 7,
                },
            },
        ]

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app",
            OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
        ), patch(
            "shifts.shift_generation.client.requests.post",
            return_value=response,
        ):
            result = generate_with_optimizer_api(context)

        self.assertEqual(
            [issue.code for issue in result.issues],
            [
                GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                GenerationIssueCode.NIGHT_COUNT_IMBALANCE,
                GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                GenerationIssueCode.DAY_ABILITY_BELOW_TARGET,
            ],
        )
        self.assertEqual(result.issues[0].dates, [target_date])
        self.assertEqual(result.issues[1].staff_ids, [self.target_staff.id])
        self.assertEqual(result.issues[2].details["incomplete_items"], ["long_streak"])
        self.assertEqual(
            result.issues[3].details["actual_ability_total"],
            5,
        )
        self.assertEqual(
            format_generation_issue(result.issues[0])[0],
            "日勤人数が設定を下回っています",
        )
        markers = build_generation_issue_markers(result.issues)
        self.assertEqual(markers.date_issue_levels[target_date], "warning")
        self.assertEqual(
            markers.staff_summary_issue_levels[
                (self.target_staff.id, "night")
            ],
            "warning",
        )

    def test_optimizer_api_client_raises_generation_issues_for_infeasible_result(self):
        context = load_generation_context(self.shift_plan)
        target_date = context.month_dates[0]
        response = Mock(status_code=200)
        response.json.return_value = {
            "status": "infeasible",
            "solver_status": "INFEASIBLE",
            "shifts": [],
            "phase_results": [],
            "issues": [
                {
                    "code": GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
                    "severity": GenerationIssueSeverity.ERROR,
                    "dates": [target_date.isoformat()],
                    "staff_ids": [],
                    "details": {
                        "available_count": 1,
                        "required_count": 2,
                    },
                },
            ],
        }

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app",
            OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
        ), patch(
            "shifts.shift_generation.client.requests.post",
            return_value=response,
        ), self.assertRaises(ShiftGenerationError) as raised:
            generate_with_optimizer_api(context)

        self.assertEqual(
            [issue.code for issue in raised.exception.issues],
            [
                GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
            ],
        )
        title, body = format_generation_issue(raised.exception.issues[0])
        self.assertEqual(title, "夜勤人数を確保できません")
        self.assertIn("夜勤2名", body)
        markers = build_generation_issue_markers(raised.exception.issues)
        self.assertEqual(markers.date_issue_levels[target_date], "error")
        self.assertEqual(
            markers.daily_summary_issue_levels[(target_date, "night")], "error"
        )

    def test_optimizer_api_client_hides_authentication_response_details(self):
        context = load_generation_context(self.shift_plan)
        api_key = secrets.token_urlsafe(32)
        response = Mock(status_code=401)

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app",
            OPTIMIZER_API_KEY=api_key,
            OPTIMIZER_API_TIMEOUT=330,
        ):
            with patch(
                "shifts.shift_generation.client.requests.post",
                return_value=response,
            ):
                with self.assertRaisesMessage(
                    OptimizerAPIError,
                    "シフト最適化サービスの認証に失敗しました。",
                ):
                    generate_with_optimizer_api(context)

    def test_optimizer_api_client_does_not_request_when_api_key_is_missing(self):
        context = load_generation_context(self.shift_plan)

        with self.settings(
            OPTIMIZER_API_URL="https://optimizer.example.run.app",
            OPTIMIZER_API_KEY="",
        ):
            with patch("shifts.shift_generation.client.requests.post") as mock_post:
                with self.assertRaisesMessage(
                    OptimizerAPIError,
                    "シフト最適化サービスの認証設定が不足しています。",
                ):
                    generate_with_optimizer_api(context)

        mock_post.assert_not_called()

    def test_optimizer_api_client_maps_http_errors(self):
        context = load_generation_context(self.shift_plan)
        cases = (
            (401, "シフト最適化サービスの認証に失敗しました。"),
            (403, "シフト最適化サービスの認証に失敗しました。"),
            (
                422,
                "シフト最適化サービスがリクエストを受け付けませんでした。条件を確認してください。",
            ),
            (
                500,
                "シフト最適化サービスでエラーが発生しました。時間をおいて再度お試しください。",
            ),
        )

        for status_code, message in cases:
            with self.subTest(status_code=status_code), self.settings(
                OPTIMIZER_API_URL="https://optimizer.example.run.app",
                OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
            ), patch(
                "shifts.shift_generation.client.requests.post",
                return_value=Mock(status_code=status_code),
            ):
                with self.assertRaisesMessage(OptimizerAPIError, message):
                    generate_with_optimizer_api(context)

    def test_optimizer_api_client_maps_transport_and_invalid_response_errors(self):
        context = load_generation_context(self.shift_plan)
        invalid_json_response = Mock(status_code=200)
        invalid_json_response.json.side_effect = ValueError()
        invalid_response = Mock(status_code=200)
        invalid_response.json.return_value = {"status": "success"}
        cases = (
            (
                requests.exceptions.Timeout(),
                "シフト最適化サービスの応答がタイムアウトしました。時間をおいて再度お試しください。",
            ),
            (
                requests.exceptions.ConnectionError(),
                "シフト最適化サービスへ接続できませんでした。時間をおいて再度お試しください。",
            ),
            (
                invalid_json_response,
                "シフト最適化サービスから不正な応答が返されました。時間をおいて再度お試しください。",
            ),
            (
                invalid_response,
                "シフト最適化サービスから不正な応答が返されました。時間をおいて再度お試しください。",
            ),
        )

        for response_or_error, message in cases:
            with self.subTest(response_or_error=type(response_or_error).__name__), self.settings(
                OPTIMIZER_API_URL="https://optimizer.example.run.app",
                OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
            ), patch("shifts.shift_generation.client.requests.post") as mock_post:
                if isinstance(response_or_error, requests.exceptions.RequestException):
                    mock_post.side_effect = response_or_error
                else:
                    mock_post.return_value = response_or_error

                with self.assertRaisesMessage(OptimizerAPIError, message):
                    generate_with_optimizer_api(context)

    def test_optimizer_api_client_rejects_invalid_shift_results(self):
        context = load_generation_context(self.shift_plan)
        base_payload = self.optimizer_api_success_response(context).json.return_value
        cases = (
            ("status", lambda payload: payload.update(status="failed")),
            ("solver_status", lambda payload: payload.pop("solver_status")),
            ("phase_results", lambda payload: payload.update(phase_results={})),
            ("issues", lambda payload: payload.update(issues={})),
            (
                "unknown_issue_code",
                lambda payload: payload["issues"][0].update(code="UNKNOWN_ISSUE"),
            ),
            (
                "invalid_issue_severity",
                lambda payload: payload["issues"][0].update(severity="fatal"),
            ),
            (
                "invalid_issue_date",
                lambda payload: payload["issues"][0].update(dates=["invalid-date"]),
            ),
            (
                "invalid_issue_staff_ids",
                lambda payload: payload["issues"][0].update(staff_ids=["1"]),
            ),
            (
                "invalid_issue_details",
                lambda payload: payload["issues"][0].update(details=[]),
            ),
            (
                "unknown_staff",
                lambda payload: payload["shifts"][0].update(staff_id=999999),
            ),
            (
                "outside_month",
                lambda payload: payload["shifts"][0].update(date="2026-08-01"),
            ),
            (
                "duplicate",
                lambda payload: payload["shifts"].append(payload["shifts"][0].copy()),
            ),
            (
                "invalid_shift_type",
                lambda payload: payload["shifts"][0].update(shift_type="不正"),
            ),
            ("missing", lambda payload: payload["shifts"].pop()),
        )

        for name, invalidate in cases:
            payload = {
                **base_payload,
                "shifts": [shift.copy() for shift in base_payload["shifts"]],
                "phase_results": list(base_payload["phase_results"]),
                "issues": [issue.copy() for issue in base_payload["issues"]],
            }
            invalidate(payload)
            response = Mock(status_code=200)
            response.json.return_value = payload
            with self.subTest(name=name), self.settings(
                OPTIMIZER_API_URL="https://optimizer.example.run.app",
                OPTIMIZER_API_KEY=secrets.token_urlsafe(32),
            ), patch(
                "shifts.shift_generation.client.requests.post",
                return_value=response,
            ):
                with self.assertRaisesMessage(
                    OptimizerAPIError,
                    "シフト最適化サービスから不正な応答が返されました。時間をおいて再度お試しください。",
                ):
                    generate_with_optimizer_api(context)


class ShiftGeneratorApiDelegationTests(SimpleTestCase):
    def test_generate_shift_always_delegates_to_optimizer_api(self):
        shift_plan = SimpleNamespace()
        context = SimpleNamespace()
        expected_result = ShiftGenerationResult(status="success", shifts=[])

        with self.settings(OPTIMIZER_API_URL=""), patch(
            "shifts.shift_generator.load_generation_context",
            return_value=context,
        ) as mock_load_context, patch(
            "shifts.shift_generator.generate_with_optimizer_api",
            return_value=expected_result,
        ) as mock_generate:
            result = generate_shift(shift_plan)

        self.assertIs(result, expected_result)
        mock_load_context.assert_called_once_with(shift_plan)
        mock_generate.assert_called_once_with(context)


class ShiftPlanCsvExportViewTests(TestCase):
    def test_csv_uses_edit_grid_labels_and_download_filename(self):
        user = get_user_model().objects.create_user(
            username="csv-export-user",
            password="password123",
        )
        shift_plan = ShiftPlan.objects.create(user=user, year=2026, month=1)
        staff_member = StaffMember.objects.create(
            user=user,
            name="佐藤 花子",
            gender=StaffMember.GenderChoices.FEMALE,
        )
        ShiftResult.objects.create(
            shift_plan=shift_plan,
            staff_member=staff_member,
            date=date(2026, 1, 1),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        )
        request = RequestFactory().get("/shifts/csv/")
        request.user = user

        response = ShiftPlanCsvExportView.as_view()(request, pk=shift_plan.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="ns_shift_2026_01.csv"',
        )
        rows = list(
            csv.reader(response.content.decode("utf-8-sig").splitlines())
        )
        self.assertEqual(rows[0][0:2], ["スタッフ", "1(木)"])
        self.assertEqual(rows[1][0:2], ["佐藤 花子", "日"])

    def test_edit_page_shows_csv_button_only_for_exportable_statuses(self):
        user = get_user_model().objects.create_user(
            username="csv-button-user",
            password="password123",
        )
        shift_plan = ShiftPlan.objects.create(user=user, year=2026, month=2)
        ShiftRule.objects.create(
            shift_plan=shift_plan,
            off_days_per_staff=8,
            max_consecutive_work_days=5,
        )
        self.client.force_login(user)
        edit_url = reverse("shifts:edit", kwargs={"pk": shift_plan.pk})

        for status, should_display in (
            (ShiftPlan.StatusChoices.DRAFT, False),
            (ShiftPlan.StatusChoices.GENERATED, True),
            (ShiftPlan.StatusChoices.CONFIRMED, True),
        ):
            with self.subTest(status=status):
                shift_plan.status = status
                shift_plan.save(update_fields=["status", "updated_at"])
                response = self.client.get(edit_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["can_export_csv"], should_display)
                if should_display:
                    self.assertContains(response, "CSVダウンロード")
                else:
                    self.assertNotContains(response, "CSVダウンロード")


class ShiftQueryCountRegressionTests(TestCase):
    """スタッフ数を増やしても主要画面の読取クエリ数が増えないことを確認する。"""

    def create_user_with_shift_data(self, username, staff_count):
        user = get_user_model().objects.create_user(username=username, password="x")
        previous_plan = ShiftPlan.objects.create(
            user=user,
            year=2026,
            month=1,
            status=ShiftPlan.StatusChoices.GENERATED,
        )
        shift_plan = ShiftPlan.objects.create(user=user, year=2026, month=2)
        ShiftRule.objects.create(
            shift_plan=shift_plan,
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=8,
            max_consecutive_work_days=5,
        )
        staff_members = StaffMember.objects.bulk_create(
            [StaffMember(user=user, name=f"スタッフ{index}") for index in range(staff_count)]
        )
        StaffRegularDayOff.objects.bulk_create(
            [
                StaffRegularDayOff(staff_member=staff_member, day_of_week=0)
                for staff_member in staff_members
            ]
        )
        ShiftResult.objects.bulk_create(
            [
                ShiftResult(
                    shift_plan=previous_plan,
                    staff_member=staff_member,
                    date=date(2026, 1, 31),
                    shift_type=ShiftResult.ShiftTypeChoices.DAY,
                )
                for staff_member in staff_members
            ]
        )
        return user, shift_plan

    def get_query_count(self, user, url):
        client = Client()
        client.force_login(user)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_key_screens_do_not_add_queries_per_staff_member(self):
        one_staff_user, one_staff_plan = self.create_user_with_shift_data(
            "shift-query-one", 1
        )
        ten_staff_user, ten_staff_plan = self.create_user_with_shift_data(
            "shift-query-ten", 10
        )

        screen_urls = (
            (
                "dashboard",
                reverse("core:main_page"),
                reverse("core:main_page"),
            ),
            (
                "shift_list",
                reverse("shifts:list"),
                reverse("shifts:list"),
            ),
            (
                "conditions",
                reverse("shifts:conditions", kwargs={"pk": one_staff_plan.pk}),
                reverse("shifts:conditions", kwargs={"pk": ten_staff_plan.pk}),
            ),
            (
                "shift_edit",
                reverse("shifts:edit", kwargs={"pk": one_staff_plan.pk}),
                reverse("shifts:edit", kwargs={"pk": ten_staff_plan.pk}),
            ),
            (
                "carryover_edit",
                reverse("shifts:carryover", kwargs={"pk": one_staff_plan.pk}),
                reverse("shifts:carryover", kwargs={"pk": ten_staff_plan.pk}),
            ),
        )

        for name, one_staff_url, ten_staff_url in screen_urls:
            with self.subTest(screen=name):
                one_staff_count = self.get_query_count(one_staff_user, one_staff_url)
                ten_staff_count = self.get_query_count(ten_staff_user, ten_staff_url)
                self.assertEqual(
                    one_staff_count,
                    ten_staff_count,
                )


class ShiftResultModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="shift-user",
            password="password123",
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="高橋 一郎",
            gender=StaffMember.GenderChoices.MALE,
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=7,
        )

    def test_paid_special_training_shift_types_can_be_saved(self):
        for shift_type in (
            ShiftResult.ShiftTypeChoices.PAID_LEAVE,
            ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
            ShiftResult.ShiftTypeChoices.TRAINING,
        ):
            with self.subTest(shift_type=shift_type):
                result = ShiftResult(
                    shift_plan=self.shift_plan,
                    staff_member=self.staff_member,
                    date=date(2026, 7, 1),
                    shift_type=shift_type,
                    input_type=ShiftResult.InputTypeChoices.MANUAL,
                )

                result.full_clean()


class ShiftAggregationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="aggregate-user",
            password="password123",
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="鈴木 明美",
            gender=StaffMember.GenderChoices.FEMALE,
        )

    def test_summary_counts_follow_requested_rules(self):
        self.staff_member.ability_level = 5
        self.staff_member.save(update_fields=["ability_level"])
        month_dates = [date(2026, 7, day) for day in range(1, 8)]
        shift_results = {
            (self.staff_member.id, month_dates[0]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[0],
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
            ),
            (self.staff_member.id, month_dates[1]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[1],
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            ),
            (self.staff_member.id, month_dates[2]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[2],
                shift_type=ShiftResult.ShiftTypeChoices.PAID_LEAVE,
            ),
            (self.staff_member.id, month_dates[3]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[3],
                shift_type=ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
            ),
            (self.staff_member.id, month_dates[4]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[4],
                shift_type=ShiftResult.ShiftTypeChoices.TRAINING,
            ),
            (self.staff_member.id, month_dates[5]): ShiftResult(
                staff_member=self.staff_member,
                date=month_dates[5],
                shift_type=ShiftResult.ShiftTypeChoices.OFF,
            ),
        }
        day_off_request_keys = {
            (self.staff_member.id, month_dates[6]): {
                "shift_type": ShiftResult.ShiftTypeChoices.OFF_REQUEST,
                "source": "day_off_request",
            }
        }

        staff_rows, day_summary_rows = build_shift_plan_grid(
            [self.staff_member],
            month_dates,
            shift_results,
            day_off_request_keys,
        )

        row_stats = staff_rows[0]["stats"]
        self.assertEqual(row_stats["day"], 1)
        self.assertEqual(row_stats["night"], 1)
        self.assertEqual(row_stats["off"], 2)
        self.assertEqual(row_stats["paid_leave"], 1)
        self.assertEqual(row_stats["special_leave"], 1)
        self.assertEqual(row_stats["training"], 1)
        self.assertEqual(day_summary_rows[0]["values"][0]["day_count"], 1)
        self.assertEqual(day_summary_rows[0]["values"][1]["night_count"], 1)
        self.assertEqual(day_summary_rows[0]["values"][4]["day_count"], 0)
        self.assertEqual(day_summary_rows[0]["values"][4]["night_count"], 0)
        self.assertEqual(day_summary_rows[0]["values"][0]["day_ability_total"], 5)
        self.assertEqual(day_summary_rows[0]["values"][1]["night_ability_total"], 5)
        self.assertEqual(day_summary_rows[0]["values"][4]["day_ability_total"], 0)
        self.assertEqual(day_summary_rows[0]["values"][4]["night_ability_total"], 0)

    def test_ability_totals_include_only_day_and_night_shifts(self):
        month_dates = [date(2026, 7, 1), date(2026, 7, 2)]
        staff_data = [
            ("日勤A", 5, ShiftResult.ShiftTypeChoices.DAY),
            ("日勤B", 3, ShiftResult.ShiftTypeChoices.DAY),
            ("夜勤A", 4, ShiftResult.ShiftTypeChoices.NIGHT),
            ("夜勤B", 2, ShiftResult.ShiftTypeChoices.NIGHT),
            ("夜勤明け", 5, ShiftResult.ShiftTypeChoices.AFTER_NIGHT),
            ("研修", 5, ShiftResult.ShiftTypeChoices.TRAINING),
            ("休み", 5, ShiftResult.ShiftTypeChoices.OFF),
        ]
        staff_members = []
        shift_results = {}
        for name, ability_level, shift_type in staff_data:
            staff_member = StaffMember.objects.create(
                user=self.user,
                name=name,
                ability_level=ability_level,
            )
            staff_members.append(staff_member)
            shift_results[(staff_member.id, month_dates[0])] = ShiftResult(
                staff_member=staff_member,
                date=month_dates[0],
                shift_type=shift_type,
            )

        _, day_summary_rows = build_shift_plan_grid(
            staff_members,
            month_dates,
            shift_results,
            {},
            daily_summary_issue_levels={
                (month_dates[0], "day_ability"): "warning",
                (month_dates[0], "night_ability"): "error",
            },
            daily_summary_issue_titles={
                (month_dates[0], "day_ability"): "日勤能力が目標を下回っています",
                (month_dates[0], "night_ability"): "夜勤能力が目標を上回っています",
            },
        )

        first_day = day_summary_rows[0]["values"][0]
        self.assertEqual(first_day["day_count"], 2)
        self.assertEqual(first_day["night_count"], 2)
        self.assertEqual(first_day["day_ability_total"], 8)
        self.assertEqual(first_day["night_ability_total"], 6)
        self.assertEqual(first_day["day_ability_issue_level"], "warning")
        self.assertEqual(first_day["night_ability_issue_level"], "error")
        self.assertEqual(
            first_day["day_ability_issue_title"], "日勤能力が目標を下回っています"
        )
        self.assertEqual(
            first_day["night_ability_issue_title"], "夜勤能力が目標を上回っています"
        )
        second_day = day_summary_rows[0]["values"][1]
        self.assertEqual(second_day["day_count"], 0)
        self.assertEqual(second_day["night_count"], 0)
        self.assertEqual(second_day["day_ability_total"], 0)
        self.assertEqual(second_day["night_ability_total"], 0)
        self.assertIsNone(second_day["day_ability_issue_level"])
        self.assertIsNone(second_day["night_ability_issue_level"])

    def test_excluded_staff_is_shown_but_not_included_in_daily_totals(self):
        month_dates = [date(2026, 7, 1), date(2026, 7, 2)]
        target_staff = StaffMember.objects.create(
            user=self.user,
            name="対象スタッフ",
            ability_level=3,
        )
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="対象外スタッフ",
            ability_level=5,
        )
        shift_results = {
            (target_staff.id, month_dates[0]): ShiftResult(
                staff_member=target_staff,
                date=month_dates[0],
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
            ),
            (excluded_staff.id, month_dates[0]): ShiftResult(
                staff_member=excluded_staff,
                date=month_dates[0],
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
            ),
            (target_staff.id, month_dates[1]): ShiftResult(
                staff_member=target_staff,
                date=month_dates[1],
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            ),
            (excluded_staff.id, month_dates[1]): ShiftResult(
                staff_member=excluded_staff,
                date=month_dates[1],
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            ),
        }

        staff_rows, day_summary_rows = build_shift_plan_grid(
            [target_staff, excluded_staff],
            month_dates,
            shift_results,
            {},
            excluded_staff_ids={excluded_staff.id},
        )

        self.assertEqual(len(staff_rows), 2)
        self.assertFalse(staff_rows[0]["is_excluded"])
        self.assertTrue(staff_rows[1]["is_excluded"])
        first_day, second_day = day_summary_rows[0]["values"]
        self.assertEqual(first_day["day_count"], 1)
        self.assertEqual(first_day["day_ability_total"], 3)
        self.assertEqual(second_day["night_count"], 1)
        self.assertEqual(second_day["night_ability_total"], 3)


class ShiftRuleFormTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rule-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )

    def test_new_rule_form_has_expected_initial_values(self):
        form = ShiftRuleForm()

        self.assertEqual(form.initial["max_consecutive_work_days"], 5)
        self.assertTrue(form.initial["night_shift_next_day_off"])

    def test_form_saves_max_consecutive_days_and_night_shift_flag(self):
        form = ShiftRuleForm(
            data={
                "required_day_staff": 4,
                "required_night_staff": 2,
                "off_days_per_staff": 9,
                "required_leader_staff": 1,
                "max_consecutive_work_days": 6,
                "night_shift_next_day_off": "True",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        shift_rule = form.save(self.shift_plan)

        self.assertEqual(shift_rule.required_leader_staff, 1)
        self.assertEqual(shift_rule.max_consecutive_work_days, 6)
        self.assertTrue(shift_rule.night_shift_next_day_off)

    def test_form_can_save_night_shift_next_day_off_as_false(self):
        form = ShiftRuleForm(
            data={
                "required_day_staff": 3,
                "required_night_staff": 1,
                "off_days_per_staff": 8,
                "required_leader_staff": "",
                "max_consecutive_work_days": 5,
                "night_shift_next_day_off": "False",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        shift_rule = form.save(self.shift_plan)

        self.assertEqual(shift_rule.required_leader_staff, 0)
        self.assertFalse(shift_rule.night_shift_next_day_off)

    def test_required_fields_are_validated(self):
        form = ShiftRuleForm(
            data={
                "required_day_staff": "",
                "required_night_staff": 1,
                "off_days_per_staff": 8,
                "required_leader_staff": "",
                "max_consecutive_work_days": 5,
                "night_shift_next_day_off": "True",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("required_day_staff", form.errors)


class EffectiveShiftRuleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="effective-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        ShiftRule.objects.create(
            shift_plan=self.shift_plan,
            required_day_staff=6,
            required_night_staff=2,
            required_leader_staff=1,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            night_shift_next_day_off=True,
        )
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            required_day_staff=7,
        )
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 8, 10),
            required_night_staff=3,
            min_ability_level=4,
            min_ability_level_staff_count=2,
        )

    def test_specific_date_overrides_weekday_and_common_rule(self):
        effective_rule = get_effective_rule_for_date(self.shift_plan, date(2026, 8, 10))

        self.assertEqual(effective_rule.required_day_staff, 7)
        self.assertEqual(effective_rule.required_day_staff_override, 7)
        self.assertEqual(effective_rule.required_night_staff, 3)
        self.assertEqual(effective_rule.required_leader_staff, 1)
        self.assertEqual(effective_rule.min_ability_level, 4)
        self.assertEqual(effective_rule.min_ability_level_staff_count, 2)

    def test_holiday_rule_overrides_weekday_rule(self):
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=WeekdayShiftRule.DayOfWeekChoices.HOLIDAY,
            required_day_staff=8,
        )

        effective_rule = get_effective_rule_for_date(self.shift_plan, date(2026, 8, 11))

        self.assertEqual(effective_rule.required_day_staff, 8)
        self.assertEqual(effective_rule.required_day_staff_override, 8)

    def test_common_day_staff_requirement_is_not_an_override(self):
        effective_rule = get_effective_rule_for_date(
            self.shift_plan, date(2026, 8, 12)
        )

        self.assertEqual(effective_rule.required_day_staff, 6)
        self.assertIsNone(effective_rule.required_day_staff_override)


class ShiftRuleWorkflowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="workflow-user",
            password="password123",
        )
        self.other_user = user_model.objects.create_user(
            username="other-user",
            password="password123",
        )
        self.client.force_login(self.user)
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        self.other_shift_plan = ShiftPlan.objects.create(
            user=self.other_user,
            year=2026,
            month=9,
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="山田 花子",
            gender=StaffMember.GenderChoices.FEMALE,
        )

    def build_conditions_post_data(self, **overrides):
        data = {
            "action": "save_conditions",
            "required_day_staff": "6",
            "required_night_staff": "2",
            "off_days_per_staff": "9",
            "required_leader_staff": "",
            "max_consecutive_work_days": "5",
            "night_shift_next_day_off": "True",
            "date_rule_total_forms": "0",
        }
        for day_of_week in range(8):
            prefix = f"weekday-{day_of_week}"
            data[f"{prefix}-selected"] = "0"
            data[f"{prefix}-day_of_week"] = str(day_of_week)
            data[f"{prefix}-required_day_staff"] = ""
            data[f"{prefix}-required_night_staff"] = ""
            data[f"{prefix}-required_leader_staff"] = ""
            data[f"{prefix}-min_ability_level"] = ""
            data[f"{prefix}-min_ability_level_staff_count"] = ""
        data.update(overrides)
        return data

    def build_date_rule_data(self, index, **overrides):
        data = {
            f"date-rule-{index}-active": "1",
            f"date-rule-{index}-date_rule_id": "",
            f"date-rule-{index}-target_date": "",
            f"date-rule-{index}-required_day_staff": "",
            f"date-rule-{index}-required_night_staff": "",
            f"date-rule-{index}-required_leader_staff": "",
            f"date-rule-{index}-min_ability_level": "",
            f"date-rule-{index}-min_ability_level_staff_count": "",
        }
        data.update(overrides)
        return data

    def test_date_rule_form_rebuild_does_not_add_queries_per_date_rule(self):
        first_rule = DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 8, 1),
            required_day_staff=1,
        )
        view = ShiftRuleEditView()

        with CaptureQueriesContext(connection) as one_rule_queries:
            one_rule_forms = view.get_date_rule_forms(
                self.shift_plan,
                data={
                    "date_rule_total_forms": "1",
                    "date-rule-0-date_rule_id": str(first_rule.pk),
                    "date-rule-0-active": "1",
                    "date-rule-0-target_date": "2026-08-01",
                    "date-rule-0-required_day_staff": "1",
                },
            )
            self.assertTrue(all(form.is_valid() for form in one_rule_forms))

        additional_rules = DateShiftRule.objects.bulk_create(
            [
                DateShiftRule(
                    shift_plan=self.shift_plan,
                    target_date=date(2026, 8, day),
                    required_day_staff=1,
                )
                for day in range(2, 11)
            ]
        )
        form_data = {"date_rule_total_forms": "10"}
        for index, date_rule in enumerate([first_rule, *additional_rules]):
            form_data[f"date-rule-{index}-date_rule_id"] = str(date_rule.pk)
            form_data[f"date-rule-{index}-active"] = "1"
            form_data[f"date-rule-{index}-target_date"] = date_rule.target_date.isoformat()
            form_data[f"date-rule-{index}-required_day_staff"] = "1"

        with CaptureQueriesContext(connection) as ten_rule_queries:
            ten_rule_forms = view.get_date_rule_forms(self.shift_plan, data=form_data)
            self.assertTrue(all(form.is_valid() for form in ten_rule_forms))

        self.assertEqual(len(one_rule_queries), len(ten_rule_queries))

    def test_create_form_has_only_year_and_month(self):
        form = ShiftPlanCreateForm(user=self.user)

        self.assertNotIn("title", form.fields)
        self.assertIn("year", form.fields)
        self.assertIn("month", form.fields)

    def test_create_form_can_save_with_year_and_month_only(self):
        form = ShiftPlanCreateForm(
            data={
                "year": "2026",
                "month": "10",
            },
            user=self.user,
        )

        self.assertTrue(form.is_valid(), form.errors)
        shift_plan = form.save(commit=False)
        shift_plan.user = self.user
        shift_plan.save()

        self.assertEqual(shift_plan.year, 2026)
        self.assertEqual(shift_plan.month, 10)
        self.assertEqual(shift_plan.display_title, "2026年10月 シフト表")

    def test_create_form_rejects_duplicate_year_and_month_for_same_user(self):
        ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=10,
        )
        form = ShiftPlanCreateForm(
            data={
                "year": "2026",
                "month": "10",
            },
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("month", form.errors)

    def create_common_rule(self):
        return ShiftRule.objects.create(
            shift_plan=self.shift_plan,
            required_day_staff=6,
            required_night_staff=2,
            required_leader_staff=1,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            night_shift_next_day_off=True,
        )

    def test_create_redirects_to_conditions(self):
        response = self.client.post(
            reverse("shifts:create"),
            {
                "year": "2026",
                "month": "10",
            },
        )

        created_shift_plan = ShiftPlan.objects.get(user=self.user, year=2026, month=10)
        self.assertRedirects(response, reverse("shifts:conditions", kwargs={"pk": created_shift_plan.pk}))

    def test_create_page_does_not_show_title_input(self):
        response = self.client.get(reverse("shifts:create"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="title"')
        self.assertNotContains(response, 'id="id_title"')
        self.assertNotContains(response, "タイトルと対象年月")
        self.assertContains(response, "対象年月を選択してください。")
        self.assertContains(response, "年")
        self.assertContains(response, "月")

    def test_edit_page_hides_lock_button_and_shows_reset_choices(self):
        self.create_common_rule()

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertNotContains(response, "現在のシフトを固定")
        self.assertContains(response, "手入力まで戻す")
        self.assertContains(response, "希望休・固定休まで戻す")

    def test_other_user_cannot_access_conditions(self):
        response = self.client.get(
            reverse("shifts:conditions", kwargs={"pk": self.other_shift_plan.pk})
        )

        self.assertEqual(response.status_code, 404)

    def test_common_conditions_can_be_saved_and_redirect_to_edit(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        shift_rule = self.shift_plan.shift_rule
        self.assertEqual(shift_rule.required_day_staff, 6)
        self.assertTrue(shift_rule.night_shift_next_day_off)

    def test_common_conditions_require_required_fields(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(required_day_staff=""),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "このフィールドは必須です。")
        self.assertFalse(ShiftRule.objects.filter(shift_plan=self.shift_plan).exists())

    def test_common_conditions_can_save_off_value(self):
        self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(night_shift_next_day_off="False"),
        )

        self.shift_plan.refresh_from_db()
        self.assertFalse(self.shift_plan.shift_rule.night_shift_next_day_off)

    def test_common_rule_re_edit_prefills_existing_values(self):
        self.create_common_rule()

        response = self.client.get(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertEqual(response.context["rule_form"].initial["required_day_staff"], 6)
        self.assertEqual(response.context["rule_form"].initial["required_night_staff"], 2)

    def test_previous_month_conditions_are_used_only_as_unsaved_initial_values(self):
        previous_plan = ShiftPlan.objects.create(user=self.user, year=2026, month=7)
        ShiftRule.objects.create(
            shift_plan=previous_plan,
            required_day_staff=7,
            required_night_staff=3,
            off_days_per_staff=10,
            required_leader_staff=2,
            max_consecutive_work_days=4,
            night_shift_next_day_off=False,
        )
        WeekdayShiftRule.objects.create(
            shift_plan=previous_plan,
            day_of_week=0,
            required_day_staff=8,
            required_night_staff=2,
            min_ability_level=4,
            min_ability_level_staff_count=2,
            memo="前月の月曜条件",
        )
        DateShiftRule.objects.create(
            shift_plan=previous_plan,
            target_date=date(2026, 7, 10),
            required_day_staff=9,
            memo="引き継がない",
        )

        response = self.client.get(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk})
        )

        rule_form = response.context["rule_form"]
        self.assertEqual(rule_form.initial["required_day_staff"], 7)
        self.assertEqual(rule_form.initial["required_night_staff"], 3)
        self.assertEqual(rule_form.initial["off_days_per_staff"], 10)
        self.assertEqual(rule_form.initial["required_leader_staff"], 2)
        self.assertEqual(rule_form.initial["max_consecutive_work_days"], 4)
        self.assertFalse(rule_form.initial["night_shift_next_day_off"])
        monday_form = response.context["weekday_form_rows"][0]["form"]
        self.assertEqual(monday_form.initial["selected"], "1")
        self.assertEqual(monday_form.initial["required_day_staff"], 8)
        self.assertNotIn("memo", monday_form.fields)
        self.assertEqual(response.context["date_rule_forms"], [])
        self.assertFalse(ShiftRule.objects.filter(shift_plan=self.shift_plan).exists())
        self.assertFalse(WeekdayShiftRule.objects.filter(shift_plan=self.shift_plan).exists())
        self.assertNotContains(response, "前月の条件を初期値として表示しています")

    def test_current_month_conditions_take_priority_over_previous_month(self):
        previous_plan = ShiftPlan.objects.create(user=self.user, year=2026, month=7)
        ShiftRule.objects.create(
            shift_plan=previous_plan, required_day_staff=9, required_night_staff=3,
            off_days_per_staff=10, max_consecutive_work_days=4,
        )
        WeekdayShiftRule.objects.create(
            shift_plan=previous_plan, day_of_week=0, required_day_staff=9,
        )
        self.create_common_rule()

        response = self.client.get(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertEqual(response.context["rule_form"].initial["required_day_staff"], 6)
        self.assertEqual(
            response.context["weekday_form_rows"][0]["form"].initial["selected"], "0"
        )

    def test_invalid_post_redisplay_keeps_posted_values_over_previous_month(self):
        previous_plan = ShiftPlan.objects.create(user=self.user, year=2026, month=7)
        ShiftRule.objects.create(
            shift_plan=previous_plan, required_day_staff=9, required_night_staff=3,
            off_days_per_staff=10, max_consecutive_work_days=4,
        )

        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(required_day_staff=""),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rule_form"].data["required_day_staff"], "")
        self.assertFalse(ShiftRule.objects.filter(shift_plan=self.shift_plan).exists())

    def test_edit_redirects_to_conditions_when_rule_missing(self):
        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertRedirects(response, reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}))

    def test_weekday_rule_can_be_saved_with_null_overrides(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "weekday-0-selected": "1",
                    "weekday-0-required_day_staff": "7",
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        weekday_rule = WeekdayShiftRule.objects.get(shift_plan=self.shift_plan, day_of_week=0)
        self.assertEqual(weekday_rule.required_day_staff, 7)
        self.assertIsNone(weekday_rule.required_night_staff)
        self.assertIsNone(weekday_rule.required_leader_staff)

    def test_weekday_rule_is_deleted_when_unselected(self):
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            required_day_staff=7,
        )

        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        self.assertFalse(
            WeekdayShiftRule.objects.filter(shift_plan=self.shift_plan, day_of_week=0).exists()
        )

    def test_weekday_rule_can_save_ability_condition(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "weekday-2-selected": "1",
                    "weekday-2-min_ability_level": "3",
                    "weekday-2-min_ability_level_staff_count": "2",
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        weekday_rule = WeekdayShiftRule.objects.get(shift_plan=self.shift_plan, day_of_week=2)
        self.assertEqual(weekday_rule.min_ability_level, 3)
        self.assertEqual(weekday_rule.min_ability_level_staff_count, 2)

    def test_holiday_rule_can_be_saved(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "weekday-7-selected": "1",
                    "weekday-7-required_day_staff": "7",
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        holiday_rule = WeekdayShiftRule.objects.get(
            shift_plan=self.shift_plan,
            day_of_week=WeekdayShiftRule.DayOfWeekChoices.HOLIDAY,
        )
        self.assertEqual(holiday_rule.required_day_staff, 7)

    def test_weekday_rule_unique_constraint_exists(self):
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            required_day_staff=7,
        )

        with self.assertRaises(IntegrityError):
            WeekdayShiftRule.objects.create(
                shift_plan=self.shift_plan,
                day_of_week=0,
                required_night_staff=2,
            )

    def test_date_rule_can_be_saved_for_date_in_target_month(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "1",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-target_date": "2026-08-12",
                            "date-rule-0-required_night_staff": "3",
                            "date-rule-0-min_ability_level": "4",
                            "date-rule-0-min_ability_level_staff_count": "2",
                        }
                    ),
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        date_rule = DateShiftRule.objects.get(shift_plan=self.shift_plan, target_date="2026-08-12")
        self.assertIsNone(date_rule.required_day_staff)
        self.assertEqual(date_rule.required_night_staff, 3)
        self.assertEqual(date_rule.min_ability_level, 4)
        self.assertEqual(date_rule.min_ability_level_staff_count, 2)

    def test_date_rule_rejects_date_outside_target_month(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "1",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-target_date": "2026-09-01",
                            "date-rule-0-required_night_staff": "3",
                        }
                    ),
                }
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "対象シフト表の年月内の日付を選択してください。")
        self.assertFalse(DateShiftRule.objects.filter(shift_plan=self.shift_plan).exists())

    def test_duplicate_date_rule_is_rejected(self):
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date="2026-08-12",
            required_night_staff=3,
        )

        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "1",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-target_date": "2026-08-12",
                            "date-rule-0-required_day_staff": "8",
                        }
                    ),
                }
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "同じ日付の特定日条件はすでに登録されています。")

    def test_date_rule_requires_both_ability_fields(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "1",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-target_date": "2026-08-12",
                            "date-rule-0-min_ability_level": "4",
                        }
                    ),
                }
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "勤務レベル条件を使う場合は、レベルと人数を両方入力してください。")

    def test_registered_date_rule_can_be_edited(self):
        date_rule = DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date="2026-08-12",
            required_night_staff=3,
        )

        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "1",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-date_rule_id": str(date_rule.pk),
                            "date-rule-0-target_date": "2026-08-12",
                            "date-rule-0-required_day_staff": "8",
                            "date-rule-0-required_night_staff": "",
                        }
                    ),
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        date_rule.refresh_from_db()
        self.assertEqual(date_rule.required_day_staff, 8)
        self.assertIsNone(date_rule.required_night_staff)

    def test_registered_date_rule_can_be_deleted(self):
        date_rule = DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date="2026-08-12",
            required_night_staff=3,
        )

        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(delete_date_rule_id=str(date_rule.pk)),
        )

        self.assertRedirects(response, reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}))
        self.assertFalse(DateShiftRule.objects.filter(pk=date_rule.pk).exists())

    def test_multiple_date_rules_can_be_saved_in_one_request(self):
        response = self.client.post(
            reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}),
            self.build_conditions_post_data(
                **{
                    "date_rule_total_forms": "2",
                    **self.build_date_rule_data(
                        0,
                        **{
                            "date-rule-0-target_date": "2026-08-12",
                            "date-rule-0-required_day_staff": "7",
                        }
                    ),
                    **self.build_date_rule_data(
                        1,
                        **{
                            "date-rule-1-target_date": "2026-08-18",
                            "date-rule-1-required_night_staff": "3",
                        }
                    ),
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}))
        self.assertEqual(DateShiftRule.objects.filter(shift_plan=self.shift_plan).count(), 2)

    def test_edit_screen_displays_saved_conditions_and_counts(self):
        self.create_common_rule()
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            required_day_staff=7,
        )
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date="2026-08-12",
            required_night_staff=3,
        )

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertContains(response, "シフト生成条件")
        self.assertContains(response, "曜日条件：1件")
        self.assertContains(response, "特定日条件：1件")
        self.assertContains(response, reverse("shifts:conditions", kwargs={"pk": self.shift_plan.pk}))
        self.assertContains(response, "勤務区分について")
        self.assertContains(response, "日別集計 日勤能力")
        self.assertContains(response, "日別集計 夜勤能力")
        self.assertContains(response, 'id="shift-generation-loading"')
        self.assertContains(response, "data-generate-shift")
        self.assertContains(response, "シフトを生成しています…")
        self.assertContains(
            response,
            "通常は数十秒で完了しますが、スタッフ数や条件によっては"
            "2〜3分程度かかる場合があります。",
        )
        self.assertContains(response, "画面を閉じずにそのままお待ちください。")
        self.assertContains(
            response,
            'event.submitter?.value !== "generate"',
        )
        self.assertContains(response, "window.requestAnimationFrame")
        self.assertContains(response, "HTMLFormElement.prototype.submit.call")

    def test_shift_save_processing_still_works(self):
        self.create_common_rule()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.DAY,
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertTrue(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date="2026-08-01",
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
            ).exists()
        )

    def test_manual_night_sets_only_after_night_with_auto_marker(self):
        self.create_common_rule()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        results = {
            result.date: result
            for result in ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date__in=[date(2026, 8, 10), date(2026, 8, 11)],
            )
        }
        self.assertEqual(results[date(2026, 8, 10)].shift_type, ShiftResult.ShiftTypeChoices.NIGHT)
        self.assertEqual(results[date(2026, 8, 11)].shift_type, ShiftResult.ShiftTypeChoices.AFTER_NIGHT)
        self.assertEqual(results[date(2026, 8, 11)].memo, "__night_shift_auto__")
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 12),
        ).exists())

    def test_false_night_rule_sets_only_after_night(self):
        rule = self.create_common_rule()
        rule.night_shift_next_day_off = False
        rule.save(update_fields=["night_shift_next_day_off"])

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        ).exists())
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 12),
        ).exists())

    def test_removing_night_clears_only_its_auto_followups(self):
        self.create_common_rule()
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        response = self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": "",
            },
        )

        self.assertRedirects(response, edit_url)
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date__in=[date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)],
        ).exists())

    def test_changing_saved_night_clears_its_auto_after_night(self):
        self.create_common_rule()
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        response = self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.DAY
                ),
            },
        )

        self.assertRedirects(response, edit_url)
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 10),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        ).exists())
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
        ).exists())

    def test_saved_auto_after_night_is_marked_for_non_editable_display(self):
        self.create_common_rule()
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        response = self.client.get(edit_url)
        row = next(
            row for row in response.context["staff_rows"]
            if row["staff_member"] == self.staff_member
        )
        after_night_cell = next(
            cell for cell in row["cells"] if cell["date"] == date(2026, 8, 11)
        )
        self.assertTrue(after_night_cell["is_night_shift_auto"])

    def test_saved_auto_after_night_cannot_be_changed_without_changing_night(self):
        self.create_common_rule()
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        response = self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-11": (
                    ShiftResult.ShiftTypeChoices.DAY
                ),
            },
        )

        self.assertRedirects(response, edit_url)
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 10),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        ).exists())
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            memo="__night_shift_auto__",
        ).exists())

    def test_removing_night_keeps_followup_changed_to_normal_shift(self):
        self.create_common_rule()
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        response = self.client.post(
            edit_url,
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": "",
                f"shift_{self.staff_member.id}_2026-08-12": (
                    ShiftResult.ShiftTypeChoices.DAY
                ),
            },
        )

        self.assertRedirects(response, edit_url)
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
        ).exists())
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 12),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        ).exists())

    def test_manual_night_at_month_end_does_not_require_hidden_followups(self):
        self.create_common_rule()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-31": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        ).exists())

    def test_edit_screen_does_not_offer_after_night_for_blank_cell(self):
        self.create_common_rule()

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertNotContains(response, '<option value="after_night"')

    def test_manual_night_can_replace_existing_shift_when_after_night_cell_is_blank(self):
        self.create_common_rule()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 10),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 10),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        ).exists())
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        ).exists())

    def test_manual_night_is_rejected_when_after_night_cell_has_existing_shift(self):
        self.create_common_rule()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-10": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "翌日の明けセルに勤務が入力されています。")
        self.assertFalse(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 10),
        ).exists())
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 11),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        ).exists())

    def test_save_keeps_generated_result_when_value_is_unchanged(self):
        self.create_common_rule()
        result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.DAY,
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        result.refresh_from_db()
        self.assertEqual(result.input_type, ShiftResult.InputTypeChoices.GENERATED)

    def test_base_fixed_cell_ignores_posted_shift_value(self):
        self.create_common_rule()
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.DAY,
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date="2026-08-01",
            ).exists()
        )

    def test_night_before_day_off_request_is_rejected(self):
        self.create_common_rule()
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-02",
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.NIGHT,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1日の夜勤は保存できません。")
        self.assertContains(response, "翌日の2日が希望休のため、夜勤明けを配置できません。")
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date="2026-08-01",
            ).exists()
        )

    def test_after_night_without_previous_night_is_rejected(self):
        self.create_common_rule()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-02": ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2日の明けは保存できません。")
        self.assertContains(response, "前日の1日に夜勤が存在しません。")
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date="2026-08-02",
            ).exists()
        )

    def test_reset_to_manual_deletes_only_generated_results(self):
        self.create_common_rule()
        manual_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        generated_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-02",
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "reset_to_manual"},
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertTrue(ShiftResult.objects.filter(pk=manual_result.pk).exists())
        self.assertFalse(ShiftResult.objects.filter(pk=generated_result.pk).exists())

    def test_reset_to_base_deletes_all_shift_results(self):
        self.create_common_rule()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-02",
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "reset_to_base"},
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        self.assertFalse(ShiftResult.objects.filter(shift_plan=self.shift_plan).exists())

    def test_base_fixed_conflict_is_shown_on_edit_screen(self):
        self.create_common_rule()
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date="2026-08-01",
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertContains(response, "競合")
        self.assertContains(response, "保存済みの「明け」と競合しています。")

    def test_night_before_regular_day_off_is_rejected(self):
        self.create_common_rule()
        StaffRegularDayOff.objects.create(
            staff_member=self.staff_member,
            day_of_week=date(2026, 8, 2).weekday(),
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.NIGHT,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "翌日の2日が曜日固定休のため、夜勤明けを配置できません。")


class ShiftCarryoverWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="carryover-workflow-user",
            password="password123",
        )
        self.client.force_login(self.user)
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="月末情報スタッフ",
        )

    def create_rule(self):
        return ShiftRule.objects.create(
            shift_plan=self.shift_plan,
            required_day_staff=0,
            required_night_staff=0,
            required_leader_staff=0,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            night_shift_next_day_off=True,
        )

    def carryover_post_data(self, *, shift_type="", consecutive_work_days="0"):
        return {
            "form-TOTAL_FORMS": "1",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "form-0-staff_member_id": str(self.staff_member.id),
            "form-0-previous_last_shift_type": shift_type,
            "form-0-previous_consecutive_work_days": consecutive_work_days,
        }

    def test_no_previous_plan_redirects_to_carryover_form_after_condition_save(self):
        self.create_rule()

        response = self.client.get(
            reverse("shifts:carryover_choice", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertRedirects(
            response,
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}),
        )

    def test_previous_plan_choice_yes_saves_previous_plan_carryover(self):
        previous_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=7,
            status=ShiftPlan.StatusChoices.GENERATED,
        )
        ShiftResult.objects.create(
            shift_plan=previous_plan,
            staff_member=self.staff_member,
            date=date(2026, 7, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )
        self.create_rule()

        response = self.client.post(
            reverse("shifts:carryover_choice", kwargs={"pk": self.shift_plan.pk}),
            {"use_previous_plan": "yes"},
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        carryover = self.shift_plan.carryovers.get(staff_member=self.staff_member)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.PREVIOUS_PLAN)
        self.assertEqual(carryover.previous_shift_plan, previous_plan)
        self.assertTrue(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date=date(2026, 8, 1),
                shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            ).exists()
        )

    def test_carryover_form_saves_excluded_staff_manual_value(self):
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="生成対象外スタッフ",
        )
        self.shift_plan.excluded_staffs.add(excluded_staff)
        self.create_rule()

        response = self.client.get(
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertContains(response, excluded_staff.name)
        response = self.client.post(
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}),
            {
                "form-TOTAL_FORMS": "2",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "1000",
                "form-0-staff_member_id": str(self.staff_member.id),
                "form-0-previous_last_shift_type": "",
                "form-0-previous_consecutive_work_days": "0",
                "form-1-staff_member_id": str(excluded_staff.id),
                "form-1-previous_last_shift_type": ShiftResult.ShiftTypeChoices.NIGHT,
                "form-1-previous_consecutive_work_days": "1",
            },
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        carryover = self.shift_plan.carryovers.get(staff_member=excluded_staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.MANUAL)
        self.assertTrue(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=excluded_staff,
                date=date(2026, 8, 1),
                shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            ).exists()
        )

    def test_carryover_form_saves_manual_none_and_consecutive_work_days(self):
        self.create_rule()

        response = self.client.post(
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}),
            self.carryover_post_data(consecutive_work_days="4"),
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        carryover = self.shift_plan.carryovers.get(staff_member=self.staff_member)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.MANUAL)
        self.assertIsNone(carryover.previous_last_shift_type)
        self.assertEqual(carryover.previous_consecutive_work_days, 4)

    def test_carryover_form_saves_manual_night_and_syncs_month_start(self):
        self.create_rule()

        response = self.client.post(
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk}),
            self.carryover_post_data(
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
                consecutive_work_days="1",
            ),
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        carryover = self.shift_plan.carryovers.get(staff_member=self.staff_member)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.MANUAL)
        self.assertEqual(
            carryover.previous_last_shift_type,
            ShiftResult.ShiftTypeChoices.NIGHT,
        )
        self.assertEqual(
            list(
                ShiftResult.objects.filter(
                    shift_plan=self.shift_plan,
                    staff_member=self.staff_member,
                )
                .order_by("date")
                .values_list("date", "shift_type")
            ),
            [
                (date(2026, 8, 1), ShiftResult.ShiftTypeChoices.AFTER_NIGHT),
                (date(2026, 8, 2), ShiftResult.ShiftTypeChoices.OFF),
            ],
        )

    def test_carryover_form_reuses_saved_manual_values(self):
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            previous_consecutive_work_days=2,
        )

        response = self.client.get(
            reverse("shifts:carryover", kwargs={"pk": self.shift_plan.pk})
        )

        form = response.context["formset"].forms[0]
        self.assertEqual(
            form.initial["previous_last_shift_type"],
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )
        self.assertEqual(form.initial["previous_consecutive_work_days"], 2)


class GenerationIssueMarkerTests(SimpleTestCase):
    def issue(self, code, severity, *, dates=None, staff_ids=None):
        return GenerationIssue(
            code=code,
            severity=severity,
            dates=dates or [],
            staff_ids=staff_ids or [],
        )

    def test_day_staffing_issues_mark_only_their_dates_and_day_totals(self):
        first_date = date(2026, 8, 3)
        second_date = date(2026, 8, 9)
        third_date = date(2026, 8, 12)

        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                    GenerationIssueSeverity.WARNING,
                    dates=[first_date],
                ),
                self.issue(
                    GenerationIssueCode.DAY_STAFFING_IMBALANCE,
                    GenerationIssueSeverity.WARNING,
                    dates=[second_date],
                ),
                self.issue(
                    GenerationIssueCode.INSUFFICIENT_LEADER_STAFF,
                    GenerationIssueSeverity.ERROR,
                    dates=[third_date],
                ),
            ]
        )

        self.assertEqual(
            markers.date_issue_levels,
            {
                first_date: "warning",
                second_date: "warning",
                third_date: "error",
            },
        )
        self.assertEqual(
            markers.daily_summary_issue_levels,
            {
                (first_date, "day"): "warning",
                (second_date, "day"): "warning",
                (third_date, "day"): "error",
            },
        )
        self.assertEqual(markers.cell_issue_levels, {})

    def test_night_count_imbalance_marks_only_night_staff_summaries(self):
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.NIGHT_COUNT_IMBALANCE,
                    GenerationIssueSeverity.WARNING,
                    staff_ids=[11, 22],
                )
            ]
        )

        self.assertEqual(
            markers.staff_summary_issue_levels,
            {(11, "night"): "warning", (22, "night"): "warning"},
        )
        self.assertEqual(markers.date_issue_levels, {})
        self.assertEqual(markers.cell_issue_levels, {})

    def test_ability_issues_mark_only_matching_ability_total_daily_summary(self):
        day_date = date(2026, 8, 5)
        night_date = date(2026, 8, 6)

        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.DAY_ABILITY_BELOW_TARGET,
                    GenerationIssueSeverity.WARNING,
                    dates=[day_date],
                ),
                self.issue(
                    GenerationIssueCode.NIGHT_ABILITY_ABOVE_TARGET,
                    GenerationIssueSeverity.WARNING,
                    dates=[night_date],
                ),
            ]
        )

        self.assertEqual(markers.date_issue_levels, {})
        self.assertEqual(
            markers.daily_summary_issue_levels,
            {
                (day_date, "day_ability"): "warning",
                (night_date, "night_ability"): "warning",
            },
        )
        self.assertEqual(markers.cell_issue_levels, {})

    def test_monthly_off_count_exceeded_marks_only_the_off_staff_summary(self):
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.MONTHLY_OFF_COUNT_EXCEEDED,
                    GenerationIssueSeverity.WARNING,
                    staff_ids=[11],
                )
            ]
        )

        self.assertEqual(
            markers.staff_summary_issue_levels,
            {(11, "off"): "warning"},
        )
        self.assertEqual(markers.date_issue_levels, {})
        self.assertEqual(markers.daily_summary_issue_levels, {})
        self.assertEqual(markers.cell_issue_levels, {})

    def test_optimization_incomplete_and_info_success_do_not_mark_the_table(self):
        target_date = date(2026, 8, 3)
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                    GenerationIssueSeverity.WARNING,
                ),
                self.issue(
                    GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED,
                    GenerationIssueSeverity.INFO,
                    dates=[target_date],
                ),
                self.issue(
                    GenerationIssueCode.SHIFT_GENERATED,
                    GenerationIssueSeverity.SUCCESS,
                ),
            ]
        )

        self.assertEqual(markers.date_issue_levels, {})
        self.assertEqual(markers.daily_summary_issue_levels, {})
        self.assertEqual(markers.cell_issue_levels, {})
        self.assertEqual(markers.staff_summary_issue_levels, {})

    def test_error_issues_mark_the_required_date_summary_and_cells(self):
        night_date = date(2026, 8, 4)
        cell_date = date(2026, 8, 5)
        off_request_date = date(2026, 8, 6)
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
                    GenerationIssueSeverity.ERROR,
                    dates=[night_date],
                ),
                self.issue(
                    GenerationIssueCode.NIGHT_SHIFT_NOT_ALLOWED,
                    GenerationIssueSeverity.ERROR,
                    dates=[cell_date],
                    staff_ids=[11],
                ),
                self.issue(
                    GenerationIssueCode.FIXED_ASSIGNMENT_CONFLICT,
                    GenerationIssueSeverity.ERROR,
                    dates=[cell_date],
                    staff_ids=[22],
                ),
                self.issue(
                    GenerationIssueCode.TOO_MANY_DAY_OFF_REQUESTS,
                    GenerationIssueSeverity.ERROR,
                    staff_ids=[11],
                ),
            ],
            off_request_cell_keys={(11, off_request_date), (11, cell_date), (22, off_request_date)},
        )

        self.assertEqual(markers.date_issue_levels, {night_date: "error"})
        self.assertEqual(
            markers.daily_summary_issue_levels,
            {(night_date, "night"): "error"},
        )
        self.assertEqual(
            markers.cell_issue_levels,
            {
                (11, cell_date): "error",
                (22, cell_date): "error",
                (11, off_request_date): "error",
            },
        )

    def test_error_wins_when_issues_overlap(self):
        target_date = date(2026, 8, 8)
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.DAY_STAFFING_IMBALANCE,
                    GenerationIssueSeverity.WARNING,
                    dates=[target_date],
                ),
                self.issue(
                    GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
                    GenerationIssueSeverity.ERROR,
                    dates=[target_date],
                ),
            ]
        )

        self.assertEqual(markers.date_issue_levels[target_date], "error")

    def test_generation_infeasible_uses_only_structured_locations(self):
        target_date = date(2026, 8, 10)
        markers = build_generation_issue_markers(
            [
                self.issue(
                    GenerationIssueCode.GENERATION_INFEASIBLE,
                    GenerationIssueSeverity.ERROR,
                    dates=[target_date],
                    staff_ids=[11],
                ),
                self.issue(
                    GenerationIssueCode.GENERATION_INFEASIBLE,
                    GenerationIssueSeverity.ERROR,
                    dates=[date(2026, 8, 11)],
                ),
            ]
        )

        self.assertEqual(markers.cell_issue_levels, {(11, target_date): "error"})
        self.assertEqual(markers.date_issue_levels, {date(2026, 8, 11): "error"})


class ShiftGenerationPersistenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="generator-save-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="保存 太郎",
            gender=StaffMember.GenderChoices.MALE,
        )

    def create_rule(self, **overrides):
        data = {
            "required_day_staff": 1,
            "required_night_staff": 0,
            "required_leader_staff": 0,
            "off_days_per_staff": 0,
            "max_consecutive_work_days": 31,
            "night_shift_next_day_off": True,
        }
        data.update(overrides)
        return ShiftRule.objects.create(shift_plan=self.shift_plan, **data)

    def test_generate_and_save_shift_persists_generated_results_and_status(self):
        self.create_rule()

        result = generate_and_save_shift(self.shift_plan)

        saved_results = ShiftResult.objects.filter(shift_plan=self.shift_plan)
        self.shift_plan.refresh_from_db()

        self.assertEqual(result.status, "success")
        self.assertEqual(saved_results.count(), 31)
        self.assertTrue(
            all(
                shift_result.input_type == ShiftResult.InputTypeChoices.GENERATED
                and not shift_result.is_locked
                for shift_result in saved_results
            )
        )
        self.assertEqual(self.shift_plan.status, ShiftPlan.StatusChoices.GENERATED)

    def test_generate_and_save_shift_keeps_manual_and_locked_results(self):
        self.create_rule(required_day_staff=0, off_days_per_staff=29)
        locked_staff = StaffMember.objects.create(
            user=self.user,
            name="固定 花子",
            gender=StaffMember.GenderChoices.FEMALE,
        )
        manual_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        locked_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=locked_staff,
            date=date(2026, 8, 2),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=True,
        )

        generate_and_save_shift(self.shift_plan)

        manual_result.refresh_from_db()
        locked_result.refresh_from_db()
        self.assertEqual(manual_result.input_type, ShiftResult.InputTypeChoices.MANUAL)
        self.assertTrue(locked_result.is_locked)

    def test_generate_and_save_shift_replaces_unlocked_generated_results(self):
        self.create_rule()
        existing_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=False,
        )

        generate_and_save_shift(self.shift_plan)

        replaced_results = ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 31),
        )
        self.assertEqual(replaced_results.count(), 1)
        self.assertNotEqual(replaced_results.get().shift_type, ShiftResult.ShiftTypeChoices.NIGHT)
        self.assertFalse(ShiftResult.objects.filter(pk=existing_result.pk).exists())

    def test_generate_and_save_shift_saves_day_off_request_as_off_request(self):
        self.create_rule(required_day_staff=0, off_days_per_staff=1)
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
        )

        generate_and_save_shift(self.shift_plan)

        saved_result = ShiftResult.objects.get(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
        )
        self.assertEqual(saved_result.shift_type, ShiftResult.ShiftTypeChoices.OFF_REQUEST)

    def test_generate_and_save_shift_rolls_back_when_generation_fails(self):
        self.create_rule(required_day_staff=0, off_days_per_staff=0)
        existing_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 5),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        )
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        with self.assertRaises(ShiftGenerationError):
            generate_and_save_shift(self.shift_plan)

        self.shift_plan.refresh_from_db()
        self.assertTrue(ShiftResult.objects.filter(pk=existing_result.pk).exists())
        self.assertEqual(self.shift_plan.status, ShiftPlan.StatusChoices.DRAFT)

    def test_generate_and_save_shift_rolls_back_when_bulk_create_fails(self):
        self.create_rule()

        with patch(
            "shifts.shift_generation.persistence.ShiftResult.objects.bulk_create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                generate_and_save_shift(self.shift_plan)

        self.shift_plan.refresh_from_db()
        self.assertEqual(self.shift_plan.status, ShiftPlan.StatusChoices.DRAFT)
        self.assertFalse(ShiftResult.objects.filter(shift_plan=self.shift_plan).exists())


class ShiftGenerationPersistenceExcludedStaffTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="excluded-persistence-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        self.target_staff = StaffMember.objects.create(
            user=self.user,
            name="生成対象スタッフ",
        )
        self.excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="対象外スタッフ",
        )

    def test_save_generated_results_keeps_excluded_staff_results(self):
        self.shift_plan.excluded_staffs.add(self.excluded_staff)
        generated_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.excluded_staff,
            date=date(2026, 8, 1),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
        )
        manual_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.excluded_staff,
            date=date(2026, 8, 2),
            shift_type=ShiftResult.ShiftTypeChoices.TRAINING,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        save_generated_shift_results(
            self.shift_plan,
            ShiftGenerationResult(status="success", shifts=[]),
        )

        self.assertTrue(ShiftResult.objects.filter(pk=generated_result.pk).exists())
        self.assertTrue(ShiftResult.objects.filter(pk=manual_result.pk).exists())

    def test_generation_does_not_create_results_for_excluded_staff(self):
        ShiftRule.objects.create(
            shift_plan=self.shift_plan,
            required_day_staff=1,
            required_night_staff=0,
            required_leader_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.shift_plan.excluded_staffs.add(self.excluded_staff)
        manual_result = ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.excluded_staff,
            date=date(2026, 8, 1),
            shift_type=ShiftResult.ShiftTypeChoices.TRAINING,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        generate_and_save_shift(self.shift_plan)

        self.assertEqual(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.target_staff,
                input_type=ShiftResult.InputTypeChoices.GENERATED,
            ).count(),
            31,
        )
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.excluded_staff,
                input_type=ShiftResult.InputTypeChoices.GENERATED,
            ).exists()
        )
        self.assertTrue(ShiftResult.objects.filter(pk=manual_result.pk).exists())


class ShiftGenerateViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="generate-view-user",
            password="password123",
        )
        self.client.force_login(self.user)
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=8,
        )
        self.staff_member = StaffMember.objects.create(
            user=self.user,
            name="生成 一郎",
            gender=StaffMember.GenderChoices.MALE,
        )

    def create_rule(self, **overrides):
        data = {
            "required_day_staff": 1,
            "required_night_staff": 0,
            "required_leader_staff": 0,
            "off_days_per_staff": 0,
            "max_consecutive_work_days": 5,
            "night_shift_next_day_off": True,
        }
        data.update(overrides)
        return ShiftRule.objects.create(shift_plan=self.shift_plan, **data)

    def create_false_month_boundary_after_night(self):
        self.create_rule(night_shift_next_day_off=False)
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )
        sync_month_boundary_assignments(self.shift_plan)

    def test_generate_action_calls_generation_service(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(status="success", shifts=[])

        with patch("shifts.views.generate_and_save_shift", return_value=fake_result) as mock_generate:
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
            )

        self.assertEqual(response.status_code, 302)
        mock_generate.assert_called_once_with(self.shift_plan)

    def test_edit_view_keeps_level_column_sticky_and_explains_reset_actions(self):
        self.create_rule()

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertContains(response, "sticky left-[198px]", count=2)
        self.assertContains(response, "font-black text-slate-700")
        self.assertContains(response, "① 自動生成のみリセット")
        self.assertContains(response, "② 初期状態にリセット")
        self.assertContains(response, "希望休・固定休だけの初期状態に戻します。")

    def test_generate_action_shows_success_message(self):
        self.create_rule(max_consecutive_work_days=31)

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "generate"},
            follow=True,
        )

        self.assertContains(response, "シフトを生成しました")
        self.assertNotContains(response, "処理時間の上限に達したため、")

    def test_generate_action_shows_structured_success_message(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                )
            ],
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=fake_result,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(response, "設定した条件をもとにシフトを生成しました。")

    def test_generate_action_preserves_issue_markers_after_redirect(self):
        self.create_rule()
        target_date = date(2026, 8, 1)
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                    severity=GenerationIssueSeverity.WARNING,
                    dates=[target_date],
                ),
            ],
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=fake_result,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        header = next(
            header
            for header in response.context["day_headers"]
            if header["date"] == target_date
        )
        daily_value = response.context["day_summary_rows"][0]["values"][0]
        self.assertEqual(header["issue_level"], "warning")
        self.assertEqual(daily_value["day_issue_level"], "warning")

    def test_generate_action_clears_previous_issues_when_regeneration_fails(self):
        self.create_rule()
        session = self.client.session
        session["generation_issues_by_shift_plan"] = {
            str(self.shift_plan.pk): [
                {
                    "code": GenerationIssueCode.DAY_STAFFING_BELOW_REQUIRED,
                    "severity": GenerationIssueSeverity.WARNING,
                    "dates": ["2026-08-01"],
                    "staff_ids": [],
                    "details": {},
                }
            ]
        }
        session.save()
        current_issue = GenerationIssue(
            code=GenerationIssueCode.GENERATION_INFEASIBLE,
            severity=GenerationIssueSeverity.ERROR,
            details={"reason": "今回の生成に失敗しました。"},
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            side_effect=ShiftGenerationError(issue=current_issue),
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "今回の生成に失敗しました。")
        self.assertEqual(response.context["date_issue_levels"], {})
        self.assertNotIn(
            str(self.shift_plan.pk),
            self.client.session.get("generation_issues_by_shift_plan", {}),
        )

    def test_monthly_off_warning_from_session_marks_only_the_off_summary(self):
        self.create_rule()
        session = self.client.session
        session["generation_issues_by_shift_plan"] = {
            str(self.shift_plan.pk): [
                {
                    "code": GenerationIssueCode.MONTHLY_OFF_COUNT_EXCEEDED,
                    "severity": GenerationIssueSeverity.WARNING,
                    "dates": [],
                    "staff_ids": [self.staff_member.id],
                    "details": {
                        "configured_off_count": 10,
                        "actual_off_count": 12,
                        "excess_count": 2,
                    },
                }
            ]
        }
        session.save()

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )

        self.assertContains(response, "月休日数が設定を超えています")
        row = next(
            row
            for row in response.context["staff_rows"]
            if row["staff_member"].id == self.staff_member.id
        )
        self.assertEqual(row["off_issue_level"], "warning")
        self.assertEqual(response.context["cell_issue_levels"], {})
        self.assertContains(response, "text-error font-bold")
        self.assertContains(response, "ring-warning/60")

    def test_generate_action_shows_each_infeasible_api_issue_and_marks_date(self):
        self.create_rule()
        target_date = date(2026, 8, 1)
        issues = [
            GenerationIssue(
                code=GenerationIssueCode.INSUFFICIENT_NIGHT_STAFF,
                severity=GenerationIssueSeverity.ERROR,
                dates=[target_date],
                details={
                    "available_count": 1,
                    "required_count": 2,
                },
            ),
            GenerationIssue(
                code=GenerationIssueCode.GENERATION_INFEASIBLE,
                severity=GenerationIssueSeverity.ERROR,
            ),
        ]

        with patch(
            "shifts.views.generate_and_save_shift",
            side_effect=ShiftGenerationError(issues=issues),
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "夜勤人数を確保できません")
        self.assertContains(response, "シフトを生成できません")
        header = next(
            header
            for header in response.context["day_headers"]
            if header["date"] == target_date
        )
        self.assertEqual(header["issue_level"], "error")
        self.assertContains(response, "ring-error/70", count=2)

    def test_generate_action_shows_day_staffing_adjustment_as_info(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED,
                    severity=GenerationIssueSeverity.INFO,
                )
            ],
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=fake_result,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
        )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(response, "日勤人数を調整しました", count=1)
        self.assertContains(response, "設定人数より多く日勤を配置しています。", count=1)
        self.assertContains(response, "alert-info", count=1)
        self.assertContains(response, 'data-alert-icon="success"', count=1)
        self.assertContains(response, 'data-alert-icon="info"', count=1)
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )

    def test_generate_action_shows_adjustment_and_incomplete_optimization(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.DAY_STAFFING_ABOVE_REQUIRED,
                    severity=GenerationIssueSeverity.INFO,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                    severity=GenerationIssueSeverity.WARNING,
                    details={
                        "incomplete_items": [
                            "day_ability_balance",
                            "night_ability_balance",
                            "long_streak",
                        ]
                    },
                ),
            ],
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=fake_result,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
        )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(response, "日勤能力配置・夜勤能力配置・連勤配置", count=1)
        self.assertContains(response, "alert-info", count=1)
        self.assertContains(response, "alert-warning", count=1)
        self.assertNotContains(response, "シフトを生成できませんでした。")

    def test_generate_action_replaces_daily_shortages_with_one_info(self):
        self.create_rule(
            required_day_staff=3,
            max_consecutive_work_days=31,
        )
        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "generate"},
            follow=True,
        )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(
            response,
            '<p class="mt-0.5 break-words text-sm font-medium leading-6 text-base-content/80">日勤人数が設定を下回っています：',
            count=1,
        )
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )

    def test_generate_action_shows_warning_message_when_violations_exist(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.DAY_STAFFING_IMBALANCE,
                    severity=GenerationIssueSeverity.WARNING,
                )
            ],
        )

        with patch("shifts.views.generate_and_save_shift", return_value=fake_result):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(response, "日勤人数にばらつきがあります")
        self.assertContains(response, "日勤人数を十分に均等化できませんでした。")
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )
        self.assertNotContains(response, "日勤が1人不足しています。")

    def test_generate_action_shows_incomplete_and_staffing_warnings_together(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            issues=[
                GenerationIssue(
                    code=GenerationIssueCode.SHIFT_GENERATED,
                    severity=GenerationIssueSeverity.SUCCESS,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.OPTIMIZATION_INCOMPLETE,
                    severity=GenerationIssueSeverity.WARNING,
                    details={"incomplete_items": ["night_ability_balance"]},
                ),
                GenerationIssue(
                    code=GenerationIssueCode.DAY_STAFFING_IMBALANCE,
                    severity=GenerationIssueSeverity.WARNING,
                ),
                GenerationIssue(
                    code=GenerationIssueCode.NIGHT_COUNT_IMBALANCE,
                    severity=GenerationIssueSeverity.WARNING,
                    details={"count_difference": 2},
                ),
            ],
        )

        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=fake_result,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        self.assertContains(response, "シフトを生成しました")
        self.assertContains(response, "夜勤能力配置", count=1)
        self.assertContains(response, "日勤人数にばらつきがあります", count=1)
        self.assertContains(response, "夜勤回数にばらつきがあります", count=1)
        self.assertContains(response, "alert-warning", count=3)
        self.assertContains(response, 'data-alert-icon="warning"', count=3)
        self.assertNotContains(response, "シフトを生成できませんでした。")

    def test_generate_action_shows_error_message_when_generation_fails(self):
        self.create_rule()

        with patch("shifts.views.generate_and_save_shift", side_effect=ShiftGenerationError("固定条件が競合しています。")):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シフトを生成できません")
        self.assertContains(response, "固定条件が競合しています。")
        self.assertContains(response, "alert-error", count=1)
        self.assertContains(response, 'data-alert-icon="error"', count=1)

    def test_generate_action_keeps_posted_manual_shift_as_fixed_input(self):
        self.create_rule(required_day_staff=0, off_days_per_staff=30)
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 8, 1),
            required_day_staff=1,
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "generate",
                f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.DAY,
            },
            follow=True,
        )

        saved_result = ShiftResult.objects.get(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
        )
        self.assertContains(response, "シフトを生成しました")
        self.assertEqual(saved_result.shift_type, ShiftResult.ShiftTypeChoices.DAY)
        self.assertEqual(saved_result.input_type, ShiftResult.InputTypeChoices.MANUAL)

    def test_generate_action_allows_first_day_after_night_without_previous_information(self):
        self.create_rule(required_day_staff=1, off_days_per_staff=0)

        with patch("shifts.views.generate_and_save_shift") as mock_generate:
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {
                    "action": "generate",
                    f"shift_{self.staff_member.id}_2026-08-01": ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
                },
            )

        self.assertEqual(response.status_code, 302)
        mock_generate.assert_called_once_with(self.shift_plan)
        self.assertTrue(ShiftResult.objects.filter(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 1),
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        ).exists())

    def test_false_boundary_allows_manual_off_on_second_day(self):
        self.create_false_month_boundary_after_night()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-02": (
                    ShiftResult.ShiftTypeChoices.OFF
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        saved_result = ShiftResult.objects.get(
            shift_plan=self.shift_plan,
            staff_member=self.staff_member,
            date=date(2026, 8, 2),
        )
        self.assertEqual(saved_result.shift_type, ShiftResult.ShiftTypeChoices.OFF)
        self.assertEqual(
            saved_result.input_type,
            ShiftResult.InputTypeChoices.MANUAL,
        )

    def test_false_boundary_allows_manual_night_on_second_day(self):
        self.create_false_month_boundary_after_night()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-02": (
                    ShiftResult.ShiftTypeChoices.NIGHT
                ),
                f"shift_{self.staff_member.id}_2026-08-03": (
                    ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        saved_shifts = dict(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date__in=[date(2026, 8, 2), date(2026, 8, 3)],
            ).values_list("date", "shift_type")
        )
        self.assertEqual(
            saved_shifts,
            {
                date(2026, 8, 2): ShiftResult.ShiftTypeChoices.NIGHT,
                date(2026, 8, 3): ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            },
        )

    def test_false_boundary_rejects_manual_day_on_second_day(self):
        self.create_false_month_boundary_after_night()

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {
                "action": "save",
                f"shift_{self.staff_member.id}_2026-08-02": (
                    ShiftResult.ShiftTypeChoices.DAY
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "前月末の夜勤明け翌日は、休みまたは夜勤にしてください。")
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.shift_plan,
                staff_member=self.staff_member,
                date=date(2026, 8, 2),
            ).exists()
        )

    def test_edit_page_marks_excluded_staff_as_not_generation_target(self):
        self.create_rule()
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="編集画面対象外",
        )
        self.shift_plan.excluded_staffs.add(excluded_staff)

        response = self.client.get(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})
        )
        row_by_staff_id = {
            row["staff_member"].id: row for row in response.context["staff_rows"]
        }

        self.assertFalse(row_by_staff_id[self.staff_member.id]["is_excluded"])
        self.assertTrue(row_by_staff_id[excluded_staff.id]["is_excluded"])
        self.assertEqual(response.context["generation_target_count"], 1)
        self.assertEqual(response.context["excluded_staff_count"], 1)
        self.assertContains(response, "data-generation-target-checkbox")

    def test_save_updates_excluded_staffs_from_generation_target_checkboxes(self):
        self.create_rule()
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="保存対象外",
        )
        edit_url = reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk})

        response = self.client.post(
            edit_url,
            {
                "action": "save",
                "generation_target_selection_submitted": "1",
                "generation_target_staff_ids": str(self.staff_member.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(self.shift_plan.excluded_staffs.values_list("id", flat=True)),
            {excluded_staff.id},
        )

        self.client.post(
            edit_url,
            {
                "action": "save",
                "generation_target_selection_submitted": "1",
                "generation_target_staff_ids": [
                    str(self.staff_member.id),
                    str(excluded_staff.id),
                ],
            },
        )

        self.assertFalse(self.shift_plan.excluded_staffs.exists())

    def test_generate_uses_posted_generation_target_selection_without_prior_save(self):
        self.create_rule()
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="生成対象外",
        )
        fake_result = ShiftGenerationResult(status="success", shifts=[])

        def generate_with_current_selection(shift_plan):
            self.assertEqual(
                set(shift_plan.excluded_staffs.values_list("id", flat=True)),
                {excluded_staff.id},
            )
            return fake_result

        with patch(
            "shifts.views.generate_and_save_shift",
            side_effect=generate_with_current_selection,
        ):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {
                    "action": "generate",
                    "generation_target_selection_submitted": "1",
                    "generation_target_staff_ids": str(self.staff_member.id),
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(self.shift_plan.excluded_staffs.values_list("id", flat=True)),
            {excluded_staff.id},
        )

    def test_generate_with_no_generation_target_does_not_call_solver(self):
        self.create_rule()
        excluded_staff = StaffMember.objects.create(
            user=self.user,
            name="全員対象外",
        )

        with patch("shifts.views.generate_and_save_shift") as mock_generate:
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {
                    "action": "generate",
                    "generation_target_selection_submitted": "1",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シフト生成対象のスタッフを1名以上選択してください。")
        mock_generate.assert_not_called()
        self.assertEqual(
            set(self.shift_plan.excluded_staffs.values_list("id", flat=True)),
            {self.staff_member.id, excluded_staff.id},
        )


class HolidayOffTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="holiday-user", password="x")
        self.client.force_login(self.user)
        self.plan = ShiftPlan.objects.create(user=self.user, year=2026, month=1)
        ShiftRule.objects.create(
            shift_plan=self.plan, required_day_staff=0, required_night_staff=0,
            off_days_per_staff=2, max_consecutive_work_days=31,
            night_shift_next_day_off=True,
        )

    def _cell(self, response, staff_member, target_date):
        row = next(
            row for row in response.context["staff_rows"]
            if row["staff_member"].id == staff_member.id
        )
        return next(cell for cell in row["cells"] if cell["date"] == target_date)

    def test_japanese_holiday_service_returns_new_years_day(self):
        self.assertIn(date(2026, 1, 1), get_japanese_holiday_dates(2026, 1))
        self.assertNotIn(date(2026, 1, 2), get_japanese_holiday_dates(2026, 1))

    def test_holiday_header_is_marked_for_red_display(self):
        headers = build_day_headers(
            [date(2026, 1, 1), date(2026, 1, 2)],
            get_japanese_holiday_dates(2026, 1),
        )
        self.assertTrue(headers[0]["is_holiday"])
        self.assertFalse(headers[1]["is_holiday"])

    def test_edit_grid_locks_only_holiday_for_enabled_staff(self):
        staff_member = StaffMember.objects.create(
            user=self.user, name="祝日固定休", is_holiday_off=True
        )
        response = self.client.get(reverse("shifts:edit", kwargs={"pk": self.plan.pk}))
        holiday_cell = self._cell(response, staff_member, date(2026, 1, 1))
        normal_cell = self._cell(response, staff_member, date(2026, 1, 2))
        self.assertTrue(holiday_cell["is_base_fixed"])
        self.assertEqual(holiday_cell["source"], "holiday_off")
        self.assertFalse(normal_cell["is_base_fixed"])

    def test_holiday_is_not_fixed_when_disabled(self):
        staff_member = StaffMember.objects.create(user=self.user, name="祝日勤務可")
        response = self.client.get(reverse("shifts:edit", kwargs={"pk": self.plan.pk}))
        self.assertFalse(
            self._cell(response, staff_member, date(2026, 1, 1))["is_base_fixed"]
        )

    def test_day_off_request_takes_priority_over_holiday(self):
        staff_member = StaffMember.objects.create(
            user=self.user, name="希望休優先", is_holiday_off=True
        )
        DayOffRequest.objects.create(
            shift_plan=self.plan, staff_member=staff_member, date=date(2026, 1, 1)
        )
        response = self.client.get(reverse("shifts:edit", kwargs={"pk": self.plan.pk}))
        cell = self._cell(response, staff_member, date(2026, 1, 1))
        self.assertEqual(cell["value"], ShiftResult.ShiftTypeChoices.OFF_REQUEST)
        self.assertEqual(cell["source"], "day_off_request")

    def test_weekday_off_and_holiday_overlap_is_one_off_cell(self):
        staff_member = StaffMember.objects.create(
            user=self.user, name="固定休重複", is_holiday_off=True
        )
        StaffRegularDayOff.objects.create(
            staff_member=staff_member, day_of_week=date(2026, 1, 1).weekday()
        )
        response = self.client.get(reverse("shifts:edit", kwargs={"pk": self.plan.pk}))
        cell = self._cell(response, staff_member, date(2026, 1, 1))
        self.assertTrue(cell["is_base_fixed"])
        self.assertEqual(cell["value"], ShiftResult.ShiftTypeChoices.OFF)

    def test_generator_fixes_holidays_as_off_and_counts_them(self):
        staff_member = StaffMember.objects.create(
            user=self.user, name="祝日生成", is_holiday_off=True
        )
        result = generate_shift(self.plan)
        shift_map = {
            (shift.staff_member_id, shift.date): shift.shift_type for shift in result.shifts
        }
        for holiday in get_japanese_holiday_dates(2026, 1):
            self.assertEqual(
                shift_map[(staff_member.id, holiday)], ShiftResult.ShiftTypeChoices.OFF
            )


class ShiftCarryoverEntryFormTests(SimpleTestCase):
    def test_shift_type_allows_zero_consecutive_work_days(self):
        for shift_type in (
            ShiftResult.ShiftTypeChoices.NIGHT,
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        ):
            with self.subTest(shift_type=shift_type):
                form = ShiftCarryoverEntryForm(
                    data={
                        "staff_member_id": "1",
                        "previous_last_shift_type": shift_type,
                        "previous_consecutive_work_days": "0",
                    }
                )

                self.assertTrue(form.is_valid(), form.errors)

    def test_none_allows_nonzero_consecutive_work_days(self):
        for shift_type, consecutive_work_days in (
            ("", "4"),
            (ShiftResult.ShiftTypeChoices.NIGHT, "1"),
            (ShiftResult.ShiftTypeChoices.AFTER_NIGHT, "1"),
        ):
            with self.subTest(shift_type=shift_type):
                form = ShiftCarryoverEntryForm(
                    data={
                        "staff_member_id": "1",
                        "previous_last_shift_type": shift_type,
                        "previous_consecutive_work_days": (
                            consecutive_work_days
                        ),
                    }
                )

                self.assertTrue(form.is_valid(), form.errors)


class ShiftCarryoverServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="carry-user", password="x")
        self.staff = StaffMember.objects.create(user=self.user, name="山田花子")
        self.previous = ShiftPlan.objects.create(
            user=self.user, year=2025, month=12, status=ShiftPlan.StatusChoices.GENERATED
        )
        self.current = ShiftPlan.objects.create(user=self.user, year=2026, month=1)

    def test_gets_previous_plan_across_year_boundary(self):
        self.assertEqual(get_usable_previous_shift_plan(self.current), self.previous)

    def test_draft_previous_plan_is_not_usable(self):
        self.previous.status = ShiftPlan.StatusChoices.DRAFT
        self.previous.save(update_fields=["status"])
        self.assertIsNone(get_usable_previous_shift_plan(self.current))

    def test_builds_carryover_and_counts_consecutive_work(self):
        for day, shift_type in ((29, "day"), (30, "night"), (31, "after_night")):
            ShiftResult.objects.create(
                shift_plan=self.previous, staff_member=self.staff,
                date=date(2025, 12, day), shift_type=shift_type,
            )
        self.assertEqual(calculate_previous_consecutive_work_days(self.previous, self.staff), 3)
        build_shift_carryovers(self.current)
        carryover = self.current.carryovers.get(staff_member=self.staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.PREVIOUS_PLAN)
        self.assertEqual(carryover.previous_consecutive_work_days, 3)

    def test_previous_plan_values_do_not_add_queries_per_staff_member(self):
        """前月末の勤務・連勤数取得はスタッフ数でクエリ数を増やさない。"""
        ShiftResult.objects.create(
            shift_plan=self.previous,
            staff_member=self.staff,
            date=date(2025, 12, 31),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        )

        with CaptureQueriesContext(connection) as one_staff_queries:
            get_previous_plan_carryover_values(self.previous, [self.staff])

        additional_staff_members = StaffMember.objects.bulk_create(
            [
                StaffMember(user=self.user, name=f"追加スタッフ{index}")
                for index in range(2, 11)
            ]
        )
        ShiftResult.objects.bulk_create(
            [
                ShiftResult(
                    shift_plan=self.previous,
                    staff_member=staff_member,
                    date=date(2025, 12, 31),
                    shift_type=ShiftResult.ShiftTypeChoices.DAY,
                )
                for staff_member in additional_staff_members
            ]
        )

        with CaptureQueriesContext(connection) as ten_staff_queries:
            get_previous_plan_carryover_values(
                self.previous,
                [self.staff, *additional_staff_members],
            )

        self.assertEqual(len(one_staff_queries), len(ten_staff_queries))

    def test_normal_build_does_not_overwrite_manual_carryover(self):
        ShiftResult.objects.create(
            shift_plan=self.previous,
            staff_member=self.staff,
            date=date(2025, 12, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_last_shift_type=None,
            previous_consecutive_work_days=4,
        )

        build_shift_carryovers(self.current)

        carryover = self.current.carryovers.get(staff_member=self.staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.MANUAL)
        self.assertIsNone(carryover.previous_last_shift_type)
        self.assertEqual(carryover.previous_consecutive_work_days, 4)

    def test_normal_build_updates_previous_plan_carryover(self):
        ShiftResult.objects.create(
            shift_plan=self.previous,
            staff_member=self.staff,
            date=date(2025, 12, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )
        build_shift_carryovers(self.current)
        ShiftResult.objects.filter(
            shift_plan=self.previous,
            staff_member=self.staff,
            date=date(2025, 12, 31),
        ).update(shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT)

        build_shift_carryovers(self.current)

        carryover = self.current.carryovers.get(staff_member=self.staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.PREVIOUS_PLAN)
        self.assertEqual(
            carryover.previous_last_shift_type,
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )

    def test_forced_build_replaces_manual_carryover_with_previous_plan(self):
        ShiftResult.objects.create(
            shift_plan=self.previous,
            staff_member=self.staff,
            date=date(2025, 12, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            previous_consecutive_work_days=2,
        )

        build_shift_carryovers(self.current, force=True)

        carryover = self.current.carryovers.get(staff_member=self.staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.PREVIOUS_PLAN)
        self.assertEqual(
            carryover.previous_last_shift_type,
            ShiftResult.ShiftTypeChoices.NIGHT,
        )

    def test_manual_carryover_is_reflected_in_month_boundary_assignments(self):
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            previous_consecutive_work_days=1,
        )

        sync_month_boundary_assignments(self.current)

        result = ShiftResult.objects.get(
            shift_plan=self.current,
            staff_member=self.staff,
            date=date(2026, 1, 1),
        )
        self.assertEqual(result.shift_type, ShiftResult.ShiftTypeChoices.OFF)

    def test_excluded_staff_manual_carryover_is_reflected_at_month_start(self):
        self.current.excluded_staffs.add(self.staff)
        ShiftRule.objects.create(
            shift_plan=self.current,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            required_day_staff=0,
            required_night_staff=0,
            night_shift_next_day_off=True,
        )
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.MANUAL,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            previous_consecutive_work_days=1,
        )

        sync_month_boundary_assignments(self.current)

        self.assertEqual(
            list(
                ShiftResult.objects.filter(
                    shift_plan=self.current,
                    staff_member=self.staff,
                )
                .order_by("date")
                .values_list("date", "shift_type")
            ),
            [
                (date(2026, 1, 1), ShiftResult.ShiftTypeChoices.AFTER_NIGHT),
                (date(2026, 1, 2), ShiftResult.ShiftTypeChoices.OFF),
            ],
        )

    def test_manual_none_with_consecutive_work_days_is_preserved(self):
        save_manual_shift_carryovers(
            self.current,
            {self.staff.id: (None, 4)},
        )

        carryover = self.current.carryovers.get(staff_member=self.staff)
        self.assertEqual(carryover.source, ShiftCarryover.SourceChoices.MANUAL)
        self.assertIsNone(carryover.previous_last_shift_type)
        self.assertEqual(carryover.previous_consecutive_work_days, 4)

    def test_previous_night_creates_locked_after_night_and_current_rule_second_day(self):
        ShiftCarryover.objects.create(
            shift_plan=self.current, staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            previous_consecutive_work_days=1,
        )
        ShiftRule.objects.create(
            shift_plan=self.current, off_days_per_staff=9, max_consecutive_work_days=5,
            required_day_staff=0, required_night_staff=0, night_shift_next_day_off=True,
        )
        results = sync_month_boundary_assignments(self.current)
        self.assertEqual({result.date: result.shift_type for result in results}, {
            date(2026, 1, 1): ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            date(2026, 1, 2): ShiftResult.ShiftTypeChoices.OFF,
        })
        self.assertTrue(all(result.is_locked for result in results))
        self.assertTrue(all(result.input_type == ShiftResult.InputTypeChoices.GENERATED for result in results))
        self.assertTrue(all(result.lock_reason == ShiftResult.LockReasonChoices.MONTH_BOUNDARY for result in results))

    def test_previous_night_with_false_rule_leaves_second_day_selectable(self):
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            previous_consecutive_work_days=1,
        )
        ShiftRule.objects.create(
            shift_plan=self.current,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            required_day_staff=0,
            required_night_staff=0,
            night_shift_next_day_off=False,
        )

        boundary_results = sync_month_boundary_assignments(self.current)
        result = generate_shift(self.current)
        shift_map = {
            shift.date: shift.shift_type for shift in result.shifts
        }

        self.assertEqual(
            {item.date: item.shift_type for item in boundary_results},
            {date(2026, 1, 1): ShiftResult.ShiftTypeChoices.AFTER_NIGHT},
        )
        self.assertEqual(
            shift_map[date(2026, 1, 2)],
            ShiftResult.ShiftTypeChoices.OFF,
        )

    def test_false_boundary_night_uses_monthly_pattern_allowance(self):
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            previous_consecutive_work_days=1,
        )
        ShiftRule.objects.create(
            shift_plan=self.current,
            off_days_per_staff=9,
            max_consecutive_work_days=5,
            required_day_staff=0,
            required_night_staff=0,
            night_shift_next_day_off=False,
        )
        DateShiftRule.objects.create(
            shift_plan=self.current,
            target_date=date(2026, 1, 2),
            required_night_staff=1,
        )
        sync_month_boundary_assignments(self.current)

        result = generate_shift(self.current)
        shift_map = {
            shift.date: shift.shift_type for shift in result.shifts
        }

        self.assertEqual(
            [shift_map[date(2026, 1, day)] for day in (1, 2)],
            [
                ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
                ShiftResult.ShiftTypeChoices.NIGHT,
            ],
        )

        for day in (10, 12):
            DateShiftRule.objects.create(
                shift_plan=self.current,
                target_date=date(2026, 1, day),
                required_night_staff=1,
            )

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.current)

    def test_previous_after_night_creates_first_day_off_idempotently(self):
        ShiftCarryover.objects.create(
            shift_plan=self.current, staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )
        sync_month_boundary_assignments(self.current)
        sync_month_boundary_assignments(self.current)
        result = ShiftResult.objects.get(shift_plan=self.current, staff_member=self.staff)
        self.assertEqual(result.date, date(2026, 1, 1))
        self.assertEqual(result.shift_type, ShiftResult.ShiftTypeChoices.OFF)

    def test_previous_after_night_uses_first_day_regular_off_without_duplicate_result(self):
        StaffRegularDayOff.objects.create(
            staff_member=self.staff,
            day_of_week=date(2026, 1, 1).weekday(),
        )
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )

        results = sync_month_boundary_assignments(self.current)

        self.assertEqual(results, [])
        self.assertFalse(
            ShiftResult.objects.filter(
                shift_plan=self.current, staff_member=self.staff, date=date(2026, 1, 1)
            ).exists()
        )

    def test_second_day_regular_off_wins_for_both_boundary_rule_values(self):
        StaffRegularDayOff.objects.create(
            staff_member=self.staff,
            day_of_week=date(2026, 1, 2).weekday(),
        )
        ShiftCarryover.objects.create(
            shift_plan=self.current,
            staff_member=self.staff,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )

        for next_day_off in (True, False):
            with self.subTest(night_shift_next_day_off=next_day_off):
                ShiftRule.objects.update_or_create(
                    shift_plan=self.current,
                    defaults={
                        "off_days_per_staff": 9,
                        "max_consecutive_work_days": 5,
                        "required_day_staff": 0,
                        "required_night_staff": 0,
                        "night_shift_next_day_off": next_day_off,
                    },
                )
                results = sync_month_boundary_assignments(self.current)
                self.assertEqual(
                    {result.date: result.shift_type for result in results},
                    {date(2026, 1, 1): ShiftResult.ShiftTypeChoices.AFTER_NIGHT},
                )
                self.assertFalse(
                    ShiftResult.objects.filter(
                        shift_plan=self.current,
                        staff_member=self.staff,
                        date=date(2026, 1, 2),
                    ).exists()
                )
