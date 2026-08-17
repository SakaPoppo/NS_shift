import csv
from dataclasses import fields
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from ortools.sat.python import cp_model

from staff.models import StaffMember, StaffRegularDayOff

from .shift_generation import optimization as shift_optimization
from .shift_generation import results as shift_results
from .forms import ShiftPlanCreateForm, ShiftRuleForm
from .shift_generator import (
    ShiftGenerationError,
    ShiftGenerationResult,
    ShiftGenerationViolation,
    ShiftGenerationViolationType,
    generate_and_save_shift,
    generate_shift,
)
from .shift_generation.optimization import (
    LONG_STREAK_WEIGHTS,
    _build_day_staffing_balance_data,
)
from .shift_generation.results import (
    _build_day_staffing_imbalance_violation,
    _build_night_count_imbalance_violation,
    build_day_staffing_adjustment_message,
    build_optimization_incomplete_message,
)
from .shift_generation.types import (
    AbilityDistributionData,
    DayStaffingBalanceData,
    OptimizationPhaseResult,
    ShiftOptimizationSummary,
)
from .models import DateShiftRule, DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule
from .services import (
    WORKLIKE_SHIFT_TYPES,
    build_shift_carryovers,
    calculate_previous_consecutive_work_days,
    get_effective_rule_for_date,
    get_japanese_holiday_dates,
    get_month_dates,
    get_usable_previous_shift_plan,
    sync_month_boundary_assignments,
)
from .views import (
    ShiftPlanCsvExportView,
    build_day_headers,
    build_shift_plan_grid,
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
        )

        first_day = day_summary_rows[0]["values"][0]
        self.assertEqual(first_day["day_count"], 2)
        self.assertEqual(first_day["night_count"], 2)
        self.assertEqual(first_day["day_ability_total"], 8)
        self.assertEqual(first_day["night_ability_total"], 6)
        second_day = day_summary_rows[0]["values"][1]
        self.assertEqual(second_day["day_count"], 0)
        self.assertEqual(second_day["night_count"], 0)
        self.assertEqual(second_day["day_ability_total"], 0)
        self.assertEqual(second_day["night_ability_total"], 0)


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
        self.assertEqual(effective_rule.required_night_staff, 3)
        self.assertEqual(effective_rule.required_leader_staff, 1)
        self.assertEqual(effective_rule.min_ability_level, 4)
        self.assertEqual(effective_rule.min_ability_level_staff_count, 2)


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
        for day_of_week in range(7):
            prefix = f"weekday-{day_of_week}"
            data[f"{prefix}-selected"] = "0"
            data[f"{prefix}-day_of_week"] = str(day_of_week)
            data[f"{prefix}-required_day_staff"] = ""
            data[f"{prefix}-required_night_staff"] = ""
            data[f"{prefix}-required_leader_staff"] = ""
            data[f"{prefix}-min_ability_level"] = ""
            data[f"{prefix}-min_ability_level_staff_count"] = ""
            data[f"{prefix}-memo"] = ""
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
            f"date-rule-{index}-memo": "",
        }
        data.update(overrides)
        return data

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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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
        self.assertEqual(monday_form.initial["memo"], "前月の月曜条件")
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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
        weekday_rule = WeekdayShiftRule.objects.get(shift_plan=self.shift_plan, day_of_week=2)
        self.assertEqual(weekday_rule.min_ability_level, 3)
        self.assertEqual(weekday_rule.min_ability_level_staff_count, 2)

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
                            "date-rule-0-memo": "処置件数が多い日",
                        }
                    ),
                }
            ),
        )

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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

        self.assertRedirects(response, reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}))
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


class ShiftGeneratorTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="generator-user",
            password="password123",
        )
        self.shift_plan = ShiftPlan.objects.create(
            user=self.user,
            year=2026,
            month=7,
        )

    def create_rule(self, **overrides):
        data = {
            "required_day_staff": 0,
            "required_night_staff": 0,
            "required_leader_staff": 0,
            "off_days_per_staff": 8,
            "max_consecutive_work_days": 5,
            "night_shift_next_day_off": True,
        }
        data.update(overrides)
        return ShiftRule.objects.create(shift_plan=self.shift_plan, **data)

    def create_staff_member(self, name="佐藤 花子", can_night_shift=True):
        return StaffMember.objects.create(
            user=self.user,
            name=name,
            gender=StaffMember.GenderChoices.FEMALE,
            can_night_shift=can_night_shift,
        )

    def build_shift_map(self, shifts):
        return {
            (generated_shift.staff_member_id, generated_shift.date): generated_shift.shift_type
            for generated_shift in shifts
        }

    def assert_max_consecutive_work_days(
        self, *, shifts, staff_members, max_consecutive_work_days
    ):
        shift_map = self.build_shift_map(shifts)
        for staff_member in staff_members:
            consecutive_work_days = 0
            for target_date in get_month_dates(
                self.shift_plan.year, self.shift_plan.month
            ):
                if shift_map[(staff_member.id, target_date)] in WORKLIKE_SHIFT_TYPES:
                    consecutive_work_days += 1
                else:
                    consecutive_work_days = 0
                self.assertLessEqual(
                    consecutive_work_days,
                    max_consecutive_work_days,
                    f"{staff_member.name} の {target_date} までの連勤が上限を超えています。",
                )

    def test_generate_shift_returns_all_days_and_exact_off_days(self):
        self.create_rule()
        staff_member = self.create_staff_member()

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(result.status, "success")
        self.assertIn(result.solver_status, {"OPTIMAL", "FEASIBLE"})
        self.assertEqual(result.staff_count, 1)
        self.assertEqual(result.target_day_count, 31)
        self.assertEqual(len(result.shifts), 31)
        self.assertEqual(
            sum(
                1
                for target_date in [date(2026, 7, day) for day in range(1, 32)]
                if shift_map[(staff_member.id, target_date)]
                in {
                    ShiftResult.ShiftTypeChoices.OFF,
                    ShiftResult.ShiftTypeChoices.OFF_REQUEST,
                }
            ),
            8,
        )

    def test_generate_shift_respects_day_off_request_and_regular_day_off(self):
        self.create_rule(off_days_per_staff=9)
        staff_member = self.create_staff_member()
        StaffRegularDayOff.objects.create(
            staff_member=staff_member,
            day_of_week=date(2026, 7, 6).weekday(),
        )
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 6),
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 6))],
            ShiftResult.ShiftTypeChoices.OFF_REQUEST,
        )
        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 13))],
            ShiftResult.ShiftTypeChoices.OFF,
        )

    def test_next_month_first_regular_off_forbids_last_day_night_without_next_plan(self):
        self.create_rule(off_days_per_staff=4)
        staff_member = self.create_staff_member()
        StaffRegularDayOff.objects.create(
            staff_member=staff_member,
            day_of_week=date(2026, 8, 1).weekday(),
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        self.assertFalse(
            ShiftPlan.objects.filter(user=self.user, year=2026, month=8).exists()
        )
        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_generate_shift_respects_manual_night_and_followup_pattern(self):
        self.create_rule()
        staff_member = self.create_staff_member()
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 7, 10),
            required_night_staff=1,
        )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 10),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 10))],
            ShiftResult.ShiftTypeChoices.NIGHT,
        )
        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 11))],
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )
        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 12))],
            ShiftResult.ShiftTypeChoices.OFF,
        )

    def test_false_night_rule_does_not_force_another_night(self):
        self.create_rule(night_shift_next_day_off=False)
        staff_member = self.create_staff_member()
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 7, 10),
            required_night_staff=1,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 10))],
            ShiftResult.ShiftTypeChoices.NIGHT,
        )
        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 11))],
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )
        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 12))],
            ShiftResult.ShiftTypeChoices.OFF,
        )

    def test_false_night_rule_allows_one_night_after_night_pattern(self):
        self.create_rule(night_shift_next_day_off=False)
        staff_member = self.create_staff_member()
        for day in (10, 12):
            DateShiftRule.objects.create(
                shift_plan=self.shift_plan,
                target_date=date(2026, 7, day),
                required_night_staff=1,
            )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            [
                shift_map[(staff_member.id, date(2026, 7, day))]
                for day in (10, 11, 12)
            ],
            [
                ShiftResult.ShiftTypeChoices.NIGHT,
                ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
                ShiftResult.ShiftTypeChoices.NIGHT,
            ],
        )

    def test_false_night_rule_rejects_two_patterns_for_one_staff(self):
        self.create_rule(night_shift_next_day_off=False)
        self.create_staff_member()
        for day in (3, 5, 12, 14):
            DateShiftRule.objects.create(
                shift_plan=self.shift_plan,
                target_date=date(2026, 7, day),
                required_night_staff=1,
            )

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_false_night_rule_respects_fixed_and_requested_off_days(self):
        self.create_rule(night_shift_next_day_off=False)
        staff_members = [
            self.create_staff_member(name="固定休スタッフ"),
            self.create_staff_member(name="希望休スタッフ"),
            self.create_staff_member(name="曜日固定休スタッフ"),
        ]
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 7, 10),
            required_night_staff=3,
        )
        for staff_member in staff_members:
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=date(2026, 7, 10),
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_members[0],
            date=date(2026, 7, 12),
            shift_type=ShiftResult.ShiftTypeChoices.OFF,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_members[1],
            date=date(2026, 7, 12),
        )
        StaffRegularDayOff.objects.create(
            staff_member=staff_members[2],
            day_of_week=date(2026, 7, 12).weekday(),
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        expected_third_day_shifts = (
            ShiftResult.ShiftTypeChoices.OFF,
            ShiftResult.ShiftTypeChoices.OFF_REQUEST,
            ShiftResult.ShiftTypeChoices.OFF,
        )
        for staff_member, expected_third_day_shift in zip(
            staff_members, expected_third_day_shifts
        ):
            with self.subTest(staff_member=staff_member.name):
                self.assertEqual(
                    shift_map[(staff_member.id, date(2026, 7, 10))],
                    ShiftResult.ShiftTypeChoices.NIGHT,
                )
                self.assertEqual(
                    shift_map[(staff_member.id, date(2026, 7, 11))],
                    ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
                )
                self.assertEqual(
                    shift_map[(staff_member.id, date(2026, 7, 12))],
                    expected_third_day_shift,
                )

    def test_false_night_rule_still_rejects_fixed_day_after_night(self):
        self.create_rule(night_shift_next_day_off=False)
        staff_member = self.create_staff_member()
        for day, shift_type in (
            (10, ShiftResult.ShiftTypeChoices.NIGHT),
            (12, ShiftResult.ShiftTypeChoices.DAY),
        ):
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=date(2026, 7, day),
                shift_type=shift_type,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        with self.assertRaisesMessage(ShiftGenerationError, "2日後の固定勤務"):
            generate_shift(self.shift_plan)

    def test_generate_shift_ignores_unlocked_generated_result(self):
        self.create_rule()
        staff_member = self.create_staff_member()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 31),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=False,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertNotEqual(
            shift_map[(staff_member.id, date(2026, 7, 31))],
            ShiftResult.ShiftTypeChoices.NIGHT,
        )

    def test_generate_shift_preserves_locked_result(self):
        self.create_rule()
        staff_member = self.create_staff_member()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 5),
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
            input_type=ShiftResult.InputTypeChoices.GENERATED,
            is_locked=True,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 5))],
            ShiftResult.ShiftTypeChoices.DAY,
        )

    def test_generate_shift_allows_first_day_fixed_after_night(self):
        self.create_rule()
        staff_member = self.create_staff_member()
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 1),
            shift_type=ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 1))],
            ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
        )

    def test_generate_shift_rejects_night_for_night_ineligible_staff(self):
        self.create_rule()
        staff_member = self.create_staff_member(can_night_shift=False)
        ShiftResult.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 10),
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
            input_type=ShiftResult.InputTypeChoices.MANUAL,
        )

        with self.assertRaisesMessage(ShiftGenerationError, "夜勤不可"):
            generate_shift(self.shift_plan)

    def test_generate_shift_rejects_excess_fixed_off_days(self):
        self.create_rule(off_days_per_staff=1)
        staff_member = self.create_staff_member()
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 1),
        )
        DayOffRequest.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            date=date(2026, 7, 2),
        )

        with self.assertRaisesMessage(ShiftGenerationError, "月休日数 1 日を超えています"):
            generate_shift(self.shift_plan)

    def test_generate_shift_matches_day_staff_requirement_when_feasible(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        staff_member = self.create_staff_member()

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(result.violations, [])
        self.assertIsNone(result.day_staffing_adjustment_message)
        self.assertEqual(
            result.optimization_summary.minimum_day_staffing_delta,
            0,
        )
        self.assertEqual(
            result.optimization_summary.maximum_day_staffing_delta,
            0,
        )
        self.assertTrue(
            all(
                shift_map[(staff_member.id, date(2026, 7, day))]
                == ShiftResult.ShiftTypeChoices.DAY
                for day in range(1, 32)
            )
        )

    def test_generate_shift_treats_day_shortage_as_adjustment_not_violation(self):
        self.create_rule(
            required_day_staff=3,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.create_staff_member()

        result = generate_shift(self.shift_plan)
        summary = result.optimization_summary
        shortages = [
            max(-delta, 0) for delta in summary.day_staffing_deltas.values()
        ]

        self.assertEqual(result.status, "success")
        self.assertEqual(sum(shortages), 62)
        self.assertEqual(max(shortages), 2)
        self.assertEqual(result.violations, [])
        self.assertFalse(result.has_violations)
        self.assertEqual(
            result.day_staffing_adjustment_message,
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "日勤数：1人で最適化を行なっています。",
        )
        self.assertNotIn("日勤が2人不足しています", str(result.violations))

    def test_generate_shift_rejects_unreachable_hard_night_requirement(self):
        self.create_rule(
            required_day_staff=0,
            required_night_staff=1,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
            night_shift_next_day_off=False,
        )
        self.create_staff_member()

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_generate_shift_does_not_warn_for_day_excess(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.create_staff_member()
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=date(2026, 7, 6).weekday(),
            required_day_staff=0,
        )

        result = generate_shift(self.shift_plan)
        total_excess = sum(
            max(delta, 0)
            for delta in result.optimization_summary.day_staffing_deltas.values()
        )

        self.assertEqual(result.violations, [])
        self.assertFalse(result.has_violations)
        self.assertGreater(total_excess, 0)
        self.assertEqual(
            result.day_staffing_adjustment_message,
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "各日の設定人数に対して0〜＋1人の範囲で最適化を行なっています。",
        )

    def test_generate_shift_applies_date_rule_before_weekday_rule_for_day_requirement(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        for index in range(3):
            self.create_staff_member(name=f"スタッフ{index + 1}")
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=date(2026, 7, 6).weekday(),
            required_day_staff=2,
        )
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 7, 6),
            required_day_staff=3,
        )

        result = generate_shift(self.shift_plan)

        self.assertEqual(
            result.optimization_summary.minimum_day_staffing_delta,
            0,
        )
        self.assertEqual(
            result.optimization_summary.maximum_day_staffing_delta,
            2,
        )
        self.assertEqual(
            result.optimization_summary.day_staffing_delta_range,
            2,
        )

    def test_generate_shift_applies_date_rule_before_weekday_rule_for_night_requirement(self):
        self.create_rule(
            required_day_staff=0,
            required_night_staff=0,
            off_days_per_staff=10,
            night_shift_next_day_off=True,
        )
        staff_members = [self.create_staff_member(name=f"夜勤スタッフ{index + 1}") for index in range(2)]
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=date(2026, 7, 6).weekday(),
            required_night_staff=1,
        )
        DateShiftRule.objects.create(
            shift_plan=self.shift_plan,
            target_date=date(2026, 7, 6),
            required_night_staff=2,
        )
        for staff_member in staff_members:
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=date(2026, 7, 6),
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        result = generate_shift(self.shift_plan)

        self.assertEqual(sum(
            shift.date == date(2026, 7, 6)
            and shift.shift_type == ShiftResult.ShiftTypeChoices.NIGHT
            for shift in result.shifts
        ), 2)

    def test_generate_shift_rejects_unreachable_required_leader_staff(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            required_leader_staff=1,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.create_staff_member(name="一般スタッフ")

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_generate_shift_rejects_unreachable_qualified_staff_requirement(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        staff_member = self.create_staff_member(name="能力不足スタッフ")
        staff_member.ability_level = 4
        staff_member.save(update_fields=["ability_level"])
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            min_ability_level=5,
            min_ability_level_staff_count=1,
        )

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_generate_shift_skips_ability_constraint_when_either_field_is_none(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        staff_member = self.create_staff_member(name="能力条件未設定スタッフ")
        staff_member.ability_level = 1
        staff_member.save(update_fields=["ability_level"])
        weekday_rule = WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
        )

        for min_ability_level, required_count in ((5, None), (None, 1)):
            with self.subTest(
                min_ability_level=min_ability_level,
                min_ability_level_staff_count=required_count,
            ):
                weekday_rule.min_ability_level = min_ability_level
                weekday_rule.min_ability_level_staff_count = required_count
                weekday_rule.save(
                    update_fields=[
                        "min_ability_level",
                        "min_ability_level_staff_count",
                    ]
                )

                result = generate_shift(self.shift_plan)

                self.assertEqual(result.status, "success")

    def test_generated_shifts_never_exceed_max_consecutive_work_days(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=8,
            max_consecutive_work_days=5,
        )
        staff_members = [
            self.create_staff_member(name="連勤確認A"),
            self.create_staff_member(name="連勤確認B"),
        ]

        result = generate_shift(self.shift_plan)

        self.assert_max_consecutive_work_days(
            shifts=result.shifts,
            staff_members=staff_members,
            max_consecutive_work_days=5,
        )

    def test_generate_shift_rejects_unavoidable_max_consecutive_work(self):
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=0)
        self.create_staff_member()

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_fixed_training_is_counted_as_work_for_max_consecutive_constraint(self):
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=2)
        staff_member = self.create_staff_member()
        for target_date, shift_type in (
            (date(2026, 7, 6), ShiftResult.ShiftTypeChoices.PAID_LEAVE),
            (date(2026, 7, 9), ShiftResult.ShiftTypeChoices.TRAINING),
            (date(2026, 7, 13), ShiftResult.ShiftTypeChoices.PAID_LEAVE),
        ):
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=target_date,
                shift_type=shift_type,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_previous_month_streak_forces_first_day_off(self):
        self.create_rule(
            required_day_staff=0,
            required_night_staff=0,
            off_days_per_staff=6,
            max_consecutive_work_days=5,
        )
        staff_member = self.create_staff_member(name="月跨ぎ連勤スタッフ")
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.DAY,
            previous_consecutive_work_days=5,
        )

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertEqual(
            shift_map[(staff_member.id, date(2026, 7, 1))],
            ShiftResult.ShiftTypeChoices.OFF,
        )

    def test_all_fixed_worklike_shift_types_count_toward_max_consecutive_constraint(self):
        staff_member = self.create_staff_member(name="固定勤務スタッフ")
        month_dates = [date(2026, 7, day) for day in range(1, 7)]

        for shift_type in WORKLIKE_SHIFT_TYPES:
            with self.subTest(shift_type=shift_type):
                model = cp_model.CpModel()
                shift_optimization._add_max_consecutive_work_constraints(
                    model=model,
                    staff_members=[staff_member],
                    month_dates=month_dates,
                    shift_vars={},
                    fixed_assignments={
                        (staff_member.id, target_date): shift_type
                        for target_date in month_dates
                    },
                    max_consecutive_work_days=5,
                    previous_consecutive_work_days={},
                )

                self.assertEqual(
                    cp_model.CpSolver().Solve(model),
                    cp_model.INFEASIBLE,
                )

    def test_special_leave_breaks_consecutive_work_hard_constraint_window(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=5,
            max_consecutive_work_days=5,
        )
        staff_member = self.create_staff_member()
        for day in (6, 12, 18, 24, 30):
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=date(2026, 7, day),
                shift_type=ShiftResult.ShiftTypeChoices.SPECIAL_LEAVE,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        result = generate_shift(self.shift_plan)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.violations, [])


class ShiftSoftOptimizationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="soft-user", password="x")
        self.shift_plan = ShiftPlan.objects.create(user=self.user, year=2026, month=2)

    def create_rule(self, **overrides):
        data = {
            "required_day_staff": 0, "required_night_staff": 0,
            "required_leader_staff": 0, "off_days_per_staff": 0,
            "max_consecutive_work_days": 31, "night_shift_next_day_off": True,
        }
        data.update(overrides)
        return ShiftRule.objects.create(shift_plan=self.shift_plan, **data)

    def create_staff_member(self, name, can_night_shift=True):
        return StaffMember.objects.create(
            user=self.user, name=name, can_night_shift=can_night_shift
        )

    def build_day_staffing_model(
        self,
        *,
        required_counts,
        total_actual_count=None,
        fixed_actual_counts=None,
        staff_count=None,
    ):
        """小規模モデルで日勤差分の均等化だけを決定的に検証する。"""

        fixed_actual_counts = fixed_actual_counts or {}
        staff_count = staff_count or max(
            max(fixed_actual_counts.values(), default=0),
            max(required_counts, default=0),
            1,
        )
        staff_members = [
            SimpleNamespace(id=index + 1) for index in range(staff_count)
        ]
        month_dates = [
            date(2026, 2, index + 1)
            for index in range(len(required_counts))
        ]
        model = cp_model.CpModel()
        shift_vars = {}
        for staff_member in staff_members:
            for target_date in month_dates:
                shift_vars[(staff_member.id, target_date)] = {
                    ShiftResult.ShiftTypeChoices.DAY: model.NewBoolVar(
                        f"day_{staff_member.id}_{target_date}"
                    )
                }
        effective_rules = {
            target_date: SimpleNamespace(required_day_staff=required_count)
            for target_date, required_count in zip(
                month_dates, required_counts
            )
        }
        data = _build_day_staffing_balance_data(
            model=model,
            staff_members=staff_members,
            month_dates=month_dates,
            shift_vars=shift_vars,
            effective_rules=effective_rules,
        )
        if total_actual_count is not None:
            model.Add(data.total_actual_day_count == total_actual_count)
        for target_date, actual_count in fixed_actual_counts.items():
            model.Add(data.actual_day_count_vars[target_date] == actual_count)
        return model, staff_members, month_dates, data

    def build_day_staffing_summary(
        self,
        *,
        minimum_delta,
        maximum_delta,
        minimum_actual,
        maximum_actual,
    ):
        return SimpleNamespace(
            minimum_day_staffing_delta=minimum_delta,
            maximum_day_staffing_delta=maximum_delta,
            day_staffing_delta_range=maximum_delta - minimum_delta,
            minimum_actual_day_count=minimum_actual,
            maximum_actual_day_count=maximum_actual,
        )

    def test_day_staffing_balance_data_keeps_current_optimization_fields(self):
        self.assertEqual(
            {item.name for item in fields(DayStaffingBalanceData)},
            {
                "actual_day_count_vars",
                "required_day_counts",
                "day_staffing_delta_vars",
                "minimum_delta",
                "maximum_delta",
                "delta_range",
                "total_actual_day_count",
                "total_required_day_count",
                "total_delta",
                "objective_score",
            },
        )

    def test_optimization_summary_keeps_current_result_fields(self):
        self.assertEqual(
            {item.name for item in fields(ShiftOptimizationSummary)},
            {
                "total_actual_day_count",
                "total_required_day_count",
                "minimum_day_staffing_delta",
                "maximum_day_staffing_delta",
                "day_staffing_delta_range",
                "minimum_actual_day_count",
                "maximum_actual_day_count",
                "actual_day_counts",
                "required_day_counts",
                "day_staffing_deltas",
                "night_shift_count_min",
                "night_shift_count_max",
                "night_count_imbalance_violation",
                "long_streak_penalty",
                "phase_statuses",
                "phase_optimal_flags",
                "night_shift_counts",
            },
        )

    def test_optimization_summary_keeps_solved_daily_staffing_values(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 6, 8],
            staff_count=10,
        )
        for target_date, actual_count in zip(month_dates, [5, 6, 10]):
            model.Add(data.actual_day_count_vars[target_date] == actual_count)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)

        summary = shift_results._build_optimization_summary(
            solver=solver,
            day_staffing_balance_data=data,
            night_count_balance_data=SimpleNamespace(
                night_count_min=None,
                night_count_max=None,
                night_balance_violation=None,
                night_count_vars={},
            ),
            long_streak_terms=[],
            phase_results=[
                OptimizationPhaseResult(
                    name="night_count_balance",
                    status="OPTIMAL",
                    objective_value=0,
                    optimal=True,
                    solver=solver,
                ),
                OptimizationPhaseResult(
                    name="day_staffing_balance",
                    status="OPTIMAL",
                    objective_value=0,
                    optimal=True,
                    solver=solver,
                ),
                OptimizationPhaseResult(
                    name="long_streak",
                    status="UNKNOWN",
                    objective_value=None,
                    optimal=False,
                    solver=None,
                ),
            ],
        )

        self.assertEqual(
            summary.required_day_counts,
            dict(zip(month_dates, [6, 6, 8])),
        )
        self.assertEqual(
            summary.actual_day_counts,
            dict(zip(month_dates, [5, 6, 10])),
        )
        self.assertEqual(
            summary.day_staffing_deltas,
            dict(zip(month_dates, [-1, 0, 2])),
        )
        self.assertIsNot(
            summary.required_day_counts,
            data.required_day_counts,
        )
        self.assertTrue(
            all(
                isinstance(value, int)
                for daily_values in (
                    summary.required_day_counts,
                    summary.actual_day_counts,
                    summary.day_staffing_deltas,
                )
                for value in daily_values.values()
            )
        )
        self.assertEqual(summary.total_actual_day_count, 21)
        self.assertEqual(summary.total_required_day_count, 20)
        self.assertEqual(summary.minimum_day_staffing_delta, -1)
        self.assertEqual(summary.maximum_day_staffing_delta, 2)
        self.assertEqual(summary.day_staffing_delta_range, 3)
        self.assertEqual(summary.minimum_actual_day_count, 5)
        self.assertEqual(summary.maximum_actual_day_count, 10)
        self.assertEqual(
            summary.phase_statuses,
            {
                "night_count_balance": "OPTIMAL",
                "day_staffing_balance": "OPTIMAL",
                "long_streak": "UNKNOWN",
            },
        )
        self.assertEqual(
            summary.phase_optimal_flags,
            {
                "night_count_balance": True,
                "day_staffing_balance": True,
                "long_streak": False,
            },
        )

    def test_day_shortage_can_be_derived_from_delta(self):
        delta = -2

        self.assertEqual(max(-delta, 0), 2)

    def test_day_excess_can_be_derived_from_delta(self):
        delta = 3

        self.assertEqual(max(delta, 0), 3)

    def test_day_staffing_adjustment_message_is_none_when_every_day_matches(self):
        summary = self.build_day_staffing_summary(
            minimum_delta=0,
            maximum_delta=0,
            minimum_actual=6,
            maximum_actual=6,
        )

        self.assertIsNone(
            build_day_staffing_adjustment_message(
                optimization_summary=summary,
                required_day_counts=[6, 6, 6, 6],
            )
        )
        self.assertEqual(
            _build_day_staffing_imbalance_violation(summary),
            [],
        )

    def test_same_requirement_adjustment_uses_actual_count_range(self):
        cases = [
            (
                (-1, 0, 5, 6),
                "設定した必要日勤数ではシフト最適化ができなかったため、"
                "日勤数：5〜6人で最適化を行なっています。",
            ),
            (
                (2, 3, 8, 9),
                "設定した必要日勤数ではシフト最適化ができなかったため、"
                "日勤数：8〜9人で最適化を行なっています。",
            ),
            (
                (-1, -1, 5, 5),
                "設定した必要日勤数ではシフト最適化ができなかったため、"
                "日勤数：5人で最適化を行なっています。",
            ),
        ]

        for values, expected_message in cases:
            with self.subTest(values=values):
                summary = self.build_day_staffing_summary(
                    minimum_delta=values[0],
                    maximum_delta=values[1],
                    minimum_actual=values[2],
                    maximum_actual=values[3],
                )
                self.assertEqual(
                    build_day_staffing_adjustment_message(
                        optimization_summary=summary,
                        required_day_counts=[6, 6, 6, 6],
                    ),
                    expected_message,
                )

    def test_varying_requirements_adjustment_uses_signed_delta_range(self):
        cases = [
            (
                (3, 3),
                "各日の設定人数に対して＋3人で最適化を行なっています。",
            ),
            (
                (2, 3),
                "各日の設定人数に対して＋2〜3人の範囲で最適化を行なっています。",
            ),
            (
                (-2, -1),
                "各日の設定人数に対して－1〜2人の範囲で最適化を行なっています。",
            ),
            (
                (-2, -2),
                "各日の設定人数に対して－2人で最適化を行なっています。",
            ),
            (
                (-1, 1),
                "各日の設定人数に対して－1〜＋1人の範囲で最適化を行なっています。",
            ),
            (
                (0, 2),
                "各日の設定人数に対して0〜＋2人の範囲で最適化を行なっています。",
            ),
            (
                (-2, 0),
                "各日の設定人数に対して－2〜0人の範囲で最適化を行なっています。",
            ),
        ]
        prefix = "設定した必要日勤数ではシフト最適化ができなかったため、"

        for (minimum_delta, maximum_delta), expected_body in cases:
            with self.subTest(
                minimum_delta=minimum_delta,
                maximum_delta=maximum_delta,
            ):
                summary = self.build_day_staffing_summary(
                    minimum_delta=minimum_delta,
                    maximum_delta=maximum_delta,
                    minimum_actual=0,
                    maximum_actual=0,
                )
                self.assertEqual(
                    build_day_staffing_adjustment_message(
                        optimization_summary=summary,
                        required_day_counts=[6, 4, 8],
                    ),
                    prefix + expected_body,
                )

    def test_incomplete_optimization_message_matches_unknown_phase(self):
        cases = [
            (
                {
                    "night_count_balance": "OPTIMAL",
                    "day_staffing_balance": "FEASIBLE",
                    "long_streak": "OPTIMAL",
                },
                None,
            ),
            (
                {"night_count_balance": "UNKNOWN"},
                None,
            ),
            (
                {"day_staffing_balance": "UNKNOWN"},
                None,
            ),
            (
                {
                    "long_streak": "UNKNOWN",
                },
                "処理時間の上限に達したため、"
                "連勤配置の調整を完了できませんでした。"
                "夜勤回数・日勤人数・能力配置まで調整したシフトを使用しています。",
            ),
        ]

        for phase_statuses, expected_message in cases:
            with self.subTest(phase_statuses=phase_statuses):
                actual_message = build_optimization_incomplete_message(
                    optimization_summary=SimpleNamespace(
                        phase_statuses=phase_statuses
                    )
                )

                self.assertEqual(actual_message, expected_message)
                if actual_message is not None:
                    self.assertNotIn("_balance", actual_message)
                    self.assertNotIn("long_streak", actual_message)

    def test_day_staffing_delta_range_one_has_no_imbalance_violation(self):
        summary = self.build_day_staffing_summary(
            minimum_delta=-1,
            maximum_delta=0,
            minimum_actual=5,
            maximum_actual=6,
        )

        self.assertEqual(
            _build_day_staffing_imbalance_violation(summary),
            [],
        )

    def test_day_staffing_delta_range_three_creates_one_monthly_violation(self):
        summary = self.build_day_staffing_summary(
            minimum_delta=-1,
            maximum_delta=2,
            minimum_actual=1,
            maximum_actual=4,
        )

        violations = _build_day_staffing_imbalance_violation(summary)

        self.assertEqual(len(violations), 1)
        violation = violations[0]
        self.assertEqual(
            violation.violation_type,
            ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE,
        )
        self.assertEqual(
            violation.message,
            "固定勤務や勤務条件の影響により、日勤人数を均等に配置できませんでした。"
            "可能な範囲で均等化しています。",
        )
        self.assertEqual(violation.minimum_count, -1)
        self.assertEqual(violation.maximum_count, 2)
        self.assertEqual(violation.count_difference, 3)
        self.assertEqual(violation.allowed_difference, 1)
        self.assertEqual(violation.amount, 2)

    def test_adjustment_message_is_not_a_violation_but_warnings_are(self):
        adjustment_only = ShiftGenerationResult(
            status="success",
            shifts=[],
            day_staffing_adjustment_message="日勤人数を調整しました。",
        )
        incomplete_optimization_only = ShiftGenerationResult(
            status="success",
            shifts=[],
            optimization_incomplete_message="能力配置の均等化は未完了です。",
        )
        day_warning = ShiftGenerationResult(
            status="success",
            shifts=[],
            violations=[
                ShiftGenerationViolation(
                    violation_type=(
                        ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE
                    ),
                    message="日勤人数を均等化できませんでした。",
                )
            ],
        )
        night_warning = ShiftGenerationResult(
            status="success",
            shifts=[],
            violations=[
                ShiftGenerationViolation(
                    violation_type=(
                        ShiftGenerationViolationType.NIGHT_COUNT_IMBALANCE
                    ),
                    message="夜勤回数を均等化できませんでした。",
                )
            ],
        )

        self.assertFalse(adjustment_only.has_violations)
        self.assertFalse(incomplete_optimization_only.has_violations)
        self.assertTrue(day_warning.has_violations)
        self.assertTrue(night_warning.has_violations)

    def test_post_generation_daily_and_consecutive_warning_builders_are_removed(self):
        for builder_name in (
            "_build_staffing_violations",
            "_build_consecutive_work_violations",
            "_build_single_consecutive_violation",
        ):
            with self.subTest(builder_name=builder_name):
                self.assertFalse(hasattr(shift_results, builder_name))
        for violation_type_name in (
            "DAY_SHORTAGE",
            "DAY_EXCESS",
            "MAX_CONSECUTIVE_WORK",
        ):
            with self.subTest(violation_type_name=violation_type_name):
                self.assertFalse(
                    hasattr(
                        ShiftGenerationViolationType,
                        violation_type_name,
                    )
                )

    def test_night_counts_are_balanced_and_hard_requirement_remains_exact(self):
        self.create_rule(
            required_day_staff=0, required_night_staff=1, off_days_per_staff=7,
            max_consecutive_work_days=31, night_shift_next_day_off=False,
        )
        staff_members = [
            self.create_staff_member(name=f"夜勤{index + 1}")
            for index in range(4)
        ]

        result = generate_shift(self.shift_plan)
        summary = result.optimization_summary
        shift_map = {
            (shift.staff_member_id, shift.date): shift.shift_type
            for shift in result.shifts
        }
        month_dates = get_month_dates(2026, 2)
        self.assertIsNotNone(summary)
        self.assertLessEqual(summary.night_shift_count_max - summary.night_shift_count_min, 1)
        for target_date in month_dates:
            self.assertEqual(sum(
                shift.date == target_date and shift.shift_type == ShiftResult.ShiftTypeChoices.NIGHT
                for shift in result.shifts
            ), 1)
        for staff_member in staff_members:
            pattern_count = sum(
                shift_map[(staff_member.id, month_dates[index])]
                == ShiftResult.ShiftTypeChoices.NIGHT
                and shift_map[(staff_member.id, month_dates[index + 1])]
                == ShiftResult.ShiftTypeChoices.AFTER_NIGHT
                and shift_map[(staff_member.id, month_dates[index + 2])]
                == ShiftResult.ShiftTypeChoices.NIGHT
                for index in range(len(month_dates) - 2)
            )
            self.assertLessEqual(pattern_count, 1)

    def test_night_imbalance_violation_amount_allows_one_shift_difference(self):
        for counts, expected_amount in (
            ({1: 4, 2: 4}, None),
            ({1: 4, 2: 3}, None),
            ({1: 4, 2: 2}, 1),
            ({1: 5, 2: 2}, 2),
        ):
            with self.subTest(counts=counts):
                violations = _build_night_count_imbalance_violation(counts)
                if expected_amount is None:
                    self.assertEqual(violations, [])
                    continue
                violation = violations[0]
                self.assertEqual(violation.amount, expected_amount)
                self.assertEqual(violation.minimum_count, min(counts.values()))
                self.assertEqual(violation.maximum_count, max(counts.values()))
                self.assertEqual(
                    violation.count_difference, max(counts.values()) - min(counts.values())
                )
                self.assertEqual(violation.allowed_difference, 1)

    def test_fixed_nights_can_exceed_ideal_spread_without_making_model_infeasible(self):
        self.create_rule(
            required_day_staff=0, required_night_staff=0, off_days_per_staff=9,
            max_consecutive_work_days=31, night_shift_next_day_off=True,
        )
        night_staff = self.create_staff_member(name="固定夜勤")
        other_staff = [
            self.create_staff_member(name="夜勤B"),
            self.create_staff_member(name="夜勤C"),
        ]
        for day in (1, 4, 7):
            DateShiftRule.objects.create(
                shift_plan=self.shift_plan,
                target_date=date(2026, 2, day),
                required_night_staff=1,
            )
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=night_staff,
                date=date(2026, 2, day),
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        result = generate_shift(self.shift_plan)

        counts = result.optimization_summary.night_shift_counts
        self.assertEqual(counts[night_staff.id], 3)
        self.assertTrue(all(counts[staff.id] == 0 for staff in other_staff))
        violation = next(
            item for item in result.violations
            if item.violation_type == ShiftGenerationViolationType.NIGHT_COUNT_IMBALANCE
        )
        self.assertEqual(violation.amount, 2)

    def test_surplus_day_slots_are_balanced_without_stopping_at_requirement(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 6, 6, 6],
            total_actual_count=36,
            staff_count=10,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(
            [solver.Value(data.actual_day_count_vars[d]) for d in month_dates],
            [9, 9, 9, 9],
        )
        self.assertEqual(solver.Value(data.delta_range), 0)
        self.assertEqual(solver.Value(data.total_actual_day_count), 36)
        self.assertEqual(data.total_required_day_count, 24)
        self.assertEqual(solver.Value(data.total_delta), 12)

    def test_shortage_day_slots_are_balanced_without_failing(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 6, 6, 6],
            total_actual_count=18,
            staff_count=6,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        actual_counts = [
            solver.Value(data.actual_day_count_vars[d]) for d in month_dates
        ]
        deltas = [
            solver.Value(data.day_staffing_delta_vars[d])
            for d in month_dates
        ]
        shortages = [max(-delta, 0) for delta in deltas]
        excesses = [max(delta, 0) for delta in deltas]
        self.assertEqual(sorted(actual_counts), [4, 4, 5, 5])
        self.assertEqual(sorted(deltas), [-2, -2, -1, -1])
        self.assertEqual(solver.Value(data.delta_range), 1)
        self.assertEqual(sum(shortages), 6)
        self.assertEqual(sum(excesses), 0)
        self.assertEqual(max(shortages), 2)

    def test_day_staffing_data_calculates_exact_source_values_and_totals(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[4, 3, 2],
            staff_count=5,
        )
        for target_date, actual_count in zip(month_dates, [2, 3, 5]):
            model.Add(data.actual_day_count_vars[target_date] == actual_count)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(data.required_day_counts, dict(zip(month_dates, [4, 3, 2])))
        self.assertEqual(
            [solver.Value(data.day_staffing_delta_vars[d]) for d in month_dates],
            [-2, 0, 3],
        )
        self.assertEqual(solver.Value(data.minimum_delta), -2)
        self.assertEqual(solver.Value(data.maximum_delta), 3)
        self.assertEqual(solver.Value(data.delta_range), 5)
        self.assertEqual(solver.Value(data.total_actual_day_count), 10)
        self.assertEqual(data.total_required_day_count, 9)
        self.assertEqual(solver.Value(data.total_delta), 1)

    def test_different_required_counts_are_balanced_by_delta(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 4, 8],
            total_actual_count=27,
            staff_count=11,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(
            [solver.Value(data.actual_day_count_vars[d]) for d in month_dates],
            [9, 7, 11],
        )
        self.assertEqual(
            [solver.Value(data.day_staffing_delta_vars[d]) for d in month_dates],
            [3, 3, 3],
        )
        self.assertEqual(solver.Value(data.delta_range), 0)

    def test_remainder_day_slots_are_distributed_with_delta_range_one(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 6, 6, 6],
            total_actual_count=34,
            staff_count=10,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        deltas = [
            solver.Value(data.day_staffing_delta_vars[d]) for d in month_dates
        ]
        self.assertEqual(sorted(deltas), [2, 2, 3, 3])
        self.assertEqual(solver.Value(data.delta_range), 1)

    def test_long_streak_phase_cannot_worsen_fixed_day_staffing_balance(self):
        model = cp_model.CpModel()
        choose_worse_balance = model.NewBoolVar("choose_worse_balance")
        day_staffing_range = 1 + choose_worse_balance
        long_streak_penalty = 1 - choose_worse_balance

        first = shift_optimization._solve_and_fix_objective(
            model=model,
            objective=day_staffing_range,
            phase_name="day_staffing_balance",
            max_time_seconds=1,
        )
        second = shift_optimization._solve_and_fix_objective(
            model=model,
            objective=long_streak_penalty,
            phase_name="long_streak",
            max_time_seconds=1,
        )

        self.assertEqual(first.objective_value, 1)
        self.assertEqual(second.solver.Value(choose_worse_balance), 0)
        self.assertEqual(second.solver.Value(day_staffing_range), 1)

    def test_optimization_phase_logs_elapsed_time_and_limit(self):
        model = cp_model.CpModel()
        objective = model.NewIntVar(0, 1, "logged_objective")

        with self.assertLogs(
            "shifts.shift_generation.optimization", level="INFO"
        ) as captured_logs:
            shift_optimization._solve_and_fix_objective(
                model=model,
                objective=objective,
                phase_name="day_staffing_balance",
                max_time_seconds=1,
            )

        self.assertRegex(
            captured_logs.output[0],
            r"phase=day_staffing_balance status=OPTIMAL elapsed=\d+\.\d{3}s limit=1s",
        )

    def test_fixed_day_outlier_can_exceed_delta_range_one(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[6, 6, 6, 6],
            total_actual_count=34,
            staff_count=10,
        )
        model.Add(data.actual_day_count_vars[month_dates[0]] == 10)
        model.Add(data.actual_day_count_vars[month_dates[1]] <= 7)
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(
            solver.Value(data.actual_day_count_vars[month_dates[0]]), 10
        )
        self.assertEqual(
            solver.Value(data.actual_day_count_vars[month_dates[1]]), 7
        )
        self.assertEqual(solver.Value(data.delta_range), 3)

    def test_fixed_day_concentration_is_preserved_and_reported(self):
        self.create_rule(
            required_day_staff=2,
            required_night_staff=0,
            off_days_per_staff=14,
            max_consecutive_work_days=31,
        )
        staff_members = [
            self.create_staff_member(name=f"固定日勤{index + 1}")
            for index in range(4)
        ]
        fixed_date = date(2026, 2, 1)
        for staff_member in staff_members:
            ShiftResult.objects.create(
                shift_plan=self.shift_plan,
                staff_member=staff_member,
                date=fixed_date,
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        result = generate_shift(self.shift_plan)

        self.assertTrue(
            all(
                shift.shift_type == ShiftResult.ShiftTypeChoices.DAY
                for shift in result.shifts
                if shift.date == fixed_date
            )
        )
        summary = result.optimization_summary
        self.assertEqual(summary.total_actual_day_count, 56)
        self.assertEqual(summary.total_required_day_count, 56)
        self.assertEqual(summary.minimum_day_staffing_delta, -1)
        self.assertEqual(summary.maximum_day_staffing_delta, 2)
        self.assertEqual(summary.day_staffing_delta_range, 3)
        self.assertEqual(summary.minimum_actual_day_count, 1)
        self.assertEqual(summary.maximum_actual_day_count, 4)
        self.assertEqual(
            set(summary.actual_day_counts),
            set(summary.required_day_counts),
        )
        self.assertEqual(
            set(summary.actual_day_counts),
            set(summary.day_staffing_deltas),
        )
        self.assertTrue(
            all(
                summary.actual_day_counts[target_date]
                - summary.required_day_counts[target_date]
                == summary.day_staffing_deltas[target_date]
                for target_date in summary.actual_day_counts
            )
        )
        day_staffing_violations = [
            violation
            for violation in result.violations
            if violation.violation_type
            == ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE
        ]
        self.assertEqual(len(day_staffing_violations), 1)

    def test_required_day_count_above_staff_count_remains_feasible(self):
        model, _, month_dates, data = self.build_day_staffing_model(
            required_counts=[5],
            total_actual_count=2,
            staff_count=2,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(
            solver.Value(data.day_staffing_delta_vars[month_dates[0]]), -3
        )
        delta = solver.Value(data.day_staffing_delta_vars[month_dates[0]])
        self.assertEqual(max(-delta, 0), 3)
        self.assertEqual(max(delta, 0), 0)
        self.assertEqual(solver.Value(data.delta_range), 0)

    def test_required_leader_and_qualified_staff_hard_constraints_are_satisfied(self):
        self.create_rule(
            required_day_staff=2,
            required_night_staff=0,
            required_leader_staff=1,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        leader = self.create_staff_member(name="リーダー")
        leader.role = StaffMember.RoleChoices.LEADER
        leader.ability_level = 2
        leader.save(update_fields=["role", "ability_level"])
        qualified = self.create_staff_member(name="能力者")
        qualified.ability_level = 5
        qualified.save(update_fields=["ability_level"])
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=0,
            min_ability_level=5,
            min_ability_level_staff_count=1,
        )

        result = generate_shift(self.shift_plan)
        shift_map = {
            (shift.staff_member_id, shift.date): shift.shift_type
            for shift in result.shifts
        }

        self.assertTrue(all(
            shift_map[(leader.id, target_date)]
            == ShiftResult.ShiftTypeChoices.DAY
            for target_date in get_month_dates(2026, 2)
        ))
        self.assertTrue(all(
            shift_map[(qualified.id, target_date)]
            == ShiftResult.ShiftTypeChoices.DAY
            for target_date in get_month_dates(2026, 2)
            if target_date.weekday() == 0
        ))

    def test_night_count_balance_data_exposes_individual_aggregate_values(self):
        month_dates = [date(2026, 2, 1), date(2026, 2, 2)]
        first_staff = self.create_staff_member(name="集計夜勤者A")
        second_staff = self.create_staff_member(name="集計夜勤者B")
        staff_members = [first_staff, second_staff]
        model = cp_model.CpModel()
        shift_vars = {}
        for staff_member in staff_members:
            for target_date in month_dates:
                shift_vars[(staff_member.id, target_date)] = {
                    ShiftResult.ShiftTypeChoices.NIGHT: model.NewBoolVar(
                        f"night_{staff_member.id}_{target_date}"
                    )
                }
        for target_date in month_dates:
            model.Add(
                shift_vars[(first_staff.id, target_date)][
                    ShiftResult.ShiftTypeChoices.NIGHT
                ]
                == 1
            )
            model.Add(
                shift_vars[(second_staff.id, target_date)][
                    ShiftResult.ShiftTypeChoices.NIGHT
                ]
                == 0
            )

        data = shift_optimization._build_night_count_balance_objective(
            model=model,
            staff_members=staff_members,
            month_dates=month_dates,
            shift_vars=shift_vars,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()
        self.assertIn(solver.Solve(model), (cp_model.OPTIMAL, cp_model.FEASIBLE))

        self.assertEqual(solver.Value(data.night_count_vars[first_staff.id]), 2)
        self.assertEqual(solver.Value(data.night_count_vars[second_staff.id]), 0)
        self.assertEqual(solver.Value(data.night_count_min), 0)
        self.assertEqual(solver.Value(data.night_count_max), 2)
        self.assertEqual(solver.Value(data.night_balance_violation), 1)
        self.assertEqual(solver.Value(data.objective_score), 1)

    def test_night_count_balance_uses_pattern_penalty_as_tiebreaker(self):
        target_date = date(2026, 2, 1)
        first_staff = self.create_staff_member(name="明け翌日夜勤候補")
        second_staff = self.create_staff_member(name="代替夜勤候補")
        staff_members = [first_staff, second_staff]
        model = cp_model.CpModel()
        first_night = model.NewBoolVar("first_night")
        second_night = model.NewBoolVar("second_night")
        model.Add(first_night + second_night == 1)
        shift_vars = {
            (first_staff.id, target_date): {
                ShiftResult.ShiftTypeChoices.NIGHT: first_night,
            },
            (second_staff.id, target_date): {
                ShiftResult.ShiftTypeChoices.NIGHT: second_night,
            },
        }

        data = shift_optimization._build_night_count_balance_objective(
            model=model,
            staff_members=staff_members,
            month_dates=[target_date],
            shift_vars=shift_vars,
            night_after_night_pattern_terms=[first_night],
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()

        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertEqual(solver.Value(data.night_balance_violation), 0)
        self.assertEqual(solver.Value(first_night), 0)
        self.assertEqual(solver.Value(second_night), 1)
        self.assertEqual(solver.Value(data.objective_score), 0)

    def test_night_count_balance_objective_is_zero_for_one_eligible_staff(self):
        model = cp_model.CpModel()
        staff_member = self.create_staff_member(name="単独夜勤者")
        target_date = date(2026, 2, 1)
        data = shift_optimization._build_night_count_balance_objective(
            model=model,
            staff_members=[staff_member],
            month_dates=[target_date],
            shift_vars={
                (staff_member.id, target_date): {
                    ShiftResult.ShiftTypeChoices.NIGHT: model.NewBoolVar(
                        "single_night"
                    )
                }
            },
        )

        self.assertIsNone(data.night_count_min)
        self.assertIsNone(data.night_count_max)
        self.assertEqual(data.night_count_vars, {})
        self.assertEqual(data.night_balance_violation, 0)
        self.assertEqual(data.objective_score, 0)

    def test_ability_distributions_use_the_correct_eligible_staff(self):
        self.create_rule()
        staff_settings = (
            ("夜勤可Lv4", 4, True),
            ("夜勤可Lv1", 1, True),
            ("夜勤不可Lv5", 5, False),
        )
        for name, ability_level, can_night_shift in staff_settings:
            staff = self.create_staff_member(
                name=name,
                can_night_shift=can_night_shift,
            )
            staff.ability_level = ability_level
            staff.save(update_fields=["ability_level"])

        with (
            patch.object(
                shift_optimization,
                "_build_night_count_balance_objective",
                wraps=(
                    shift_optimization._build_night_count_balance_objective
                ),
            ) as build_night_objective,
            patch.object(
                shift_optimization,
                "_build_day_staffing_balance_data",
                wraps=(
                    shift_optimization._build_day_staffing_balance_data
                ),
            ) as build_day_objective,
        ):
            result = generate_shift(self.shift_plan)

        build_night_objective.assert_called_once()
        build_day_objective.assert_called_once()
        night_distribution_data = build_night_objective.call_args.kwargs[
            "ability_distribution_data"
        ]
        day_distribution_data = build_day_objective.call_args.kwargs[
            "ability_distribution_data"
        ]
        self.assertEqual(result.status, "success")
        self.assertEqual(
            night_distribution_data.shift_type,
            ShiftResult.ShiftTypeChoices.NIGHT,
        )
        self.assertEqual(night_distribution_data.eligible_staff_count, 2)
        self.assertEqual(
            night_distribution_data.eligible_above_counts,
            {3: 1, 4: 1, 5: 0},
        )
        self.assertEqual(
            day_distribution_data.shift_type,
            ShiftResult.ShiftTypeChoices.DAY,
        )
        self.assertEqual(day_distribution_data.eligible_staff_count, 3)
        self.assertEqual(
            day_distribution_data.eligible_above_counts,
            {3: 2, 4: 2, 5: 1},
        )

    def test_phase_definitions_keep_required_order_and_time_limits(self):
        phase_definitions = shift_optimization._build_phase_definitions(
            day_staffing_balance_data=DayStaffingBalanceData(
                objective_score=1
            ),
            night_count_balance_data=SimpleNamespace(objective_score=4),
            long_streak_terms=[7],
            staff_count=20,
        )

        self.assertEqual(
            [phase.name for phase in phase_definitions],
            [
                "night_count_balance",
                "day_staffing_balance",
                "long_streak",
            ],
        )
        self.assertEqual(
            shift_optimization.REQUIRED_OPTIMIZATION_PHASES,
            {"night_count_balance", "day_staffing_balance"},
        )
        self.assertTrue(all(
            phase.max_time_seconds
            == shift_optimization.PHASE_TIME_LIMITS[phase.name]
            for phase in phase_definitions
        ))

    def test_shift_solution_hints_clear_then_include_every_shift_variable(self):
        shift_vars = {
            (1, date(2026, 2, 1)): {"day": object(), "night": object()},
            (2, date(2026, 2, 2)): {"day": object(), "off": object()},
        }
        all_vars = [
            shift_var
            for day_vars in shift_vars.values()
            for shift_var in day_vars.values()
        ]
        hint_values = {
            shift_var: index % 2
            for index, shift_var in enumerate(all_vars)
        }
        events = []

        class HintModel:
            def ClearHints(self):
                events.append(("clear",))

            def AddHint(self, shift_var, value):
                events.append(("add", shift_var, value))

        class HintSolver:
            def Value(self, shift_var):
                return hint_values[shift_var]

        shift_optimization._add_shift_solution_hints(
            model=HintModel(),
            shift_vars=shift_vars,
            solver=HintSolver(),
        )

        self.assertEqual(events[0], ("clear",))
        self.assertEqual(
            events[1:],
            [
                ("add", shift_var, hint_values[shift_var])
                for shift_var in all_vars
            ],
        )

    def test_all_successful_phases_use_latest_solver_and_keep_hints(self):
        phase_names = list(shift_optimization.PHASE_TIME_LIMITS)
        solvers = [object() for _ in phase_names]
        results = [
            OptimizationPhaseResult(
                name=phase_name,
                status="OPTIMAL",
                objective_value=index,
                optimal=True,
                solver=solvers[index],
            )
            for index, phase_name in enumerate(phase_names)
        ]
        phases = [
            SimpleNamespace(name=result.name, objective=index, max_time_seconds=1)
            for index, result in enumerate(results)
        ]

        with (
            patch.object(
                shift_optimization,
                "_solve_and_fix_objective",
                side_effect=results,
            ),
            patch.object(
                shift_optimization,
                "_add_shift_solution_hints",
            ) as add_hints,
        ):
            phase_results, solver, status = (
                shift_optimization._run_optimization_phases(
                    model=object(),
                    shift_vars={"shifts": "only"},
                    phase_definitions=phases,
                )
            )

        self.assertEqual(phase_results, results)
        self.assertIs(solver, solvers[-1])
        self.assertEqual(status, "OPTIMAL")
        self.assertEqual(add_hints.call_count, len(phases) - 1)
        self.assertEqual(
            [call.kwargs["solver"] for call in add_hints.call_args_list],
            solvers[:-1],
        )

    def test_feasible_result_updates_last_successful_solver_in_every_phase(self):
        phase_names = list(shift_optimization.PHASE_TIME_LIMITS)
        phases = [
            SimpleNamespace(name=name, objective=index, max_time_seconds=1)
            for index, name in enumerate(phase_names)
        ]

        for feasible_index, feasible_phase_name in enumerate(phase_names):
            with self.subTest(feasible_phase=feasible_phase_name):
                solvers = [object() for _ in phase_names]
                results = [
                    OptimizationPhaseResult(
                        name=phase_name,
                        status=(
                            "FEASIBLE"
                            if index == feasible_index
                            else "OPTIMAL"
                        ),
                        objective_value=index,
                        optimal=index != feasible_index,
                        solver=solvers[index],
                    )
                    for index, phase_name in enumerate(phase_names)
                ]
                with (
                    patch.object(
                        shift_optimization,
                        "_solve_and_fix_objective",
                        side_effect=results,
                    ),
                    patch.object(
                        shift_optimization,
                        "_add_shift_solution_hints",
                    ) as add_hints,
                ):
                    phase_results, solver, status = (
                        shift_optimization._run_optimization_phases(
                            model=object(),
                            shift_vars={},
                            phase_definitions=phases,
                        )
                    )

                self.assertEqual(phase_results, results)
                self.assertIs(solver, solvers[-1])
                self.assertEqual(status, results[-1].status)
                if feasible_index < len(phase_names) - 1:
                    self.assertIs(
                        add_hints.call_args_list[feasible_index].kwargs[
                            "solver"
                        ],
                        solvers[feasible_index],
                    )

    def test_later_unknown_uses_previous_solver_and_marks_remaining_phases(self):
        phase_names = list(shift_optimization.PHASE_TIME_LIMITS)
        phases = [
            SimpleNamespace(name=name, objective=index, max_time_seconds=1)
            for index, name in enumerate(phase_names)
        ]
        successful_solvers = [object(), object()]
        executed_results = [
            OptimizationPhaseResult(
                name=phase_names[index],
                status="OPTIMAL",
                objective_value=index,
                optimal=True,
                solver=successful_solvers[index],
            )
            for index in range(2)
        ]
        executed_results.append(
            OptimizationPhaseResult(
                name="long_streak",
                status="UNKNOWN",
                objective_value=None,
                optimal=False,
                solver=None,
            )
        )
        with (
            patch.object(
                shift_optimization,
                "_solve_and_fix_objective",
                side_effect=executed_results,
            ) as solve_phase,
            patch.object(
                shift_optimization,
                "_add_shift_solution_hints",
            ) as add_hints,
        ):
            phase_results, solver, status = (
                shift_optimization._run_optimization_phases(
                    model=object(),
                    shift_vars={},
                    phase_definitions=phases,
                )
            )

        self.assertIs(solver, successful_solvers[-1])
        self.assertEqual(status, "OPTIMAL")
        self.assertEqual(
            {result.name: result.status for result in phase_results},
            {
                "night_count_balance": "OPTIMAL",
                "day_staffing_balance": "OPTIMAL",
                "long_streak": "UNKNOWN",
            },
        )
        self.assertEqual(solve_phase.call_count, 3)
        self.assertEqual(add_hints.call_count, 2)
        self.assertIsNone(phase_results[-1].solver)

    def test_day_staffing_unknown_still_raises_generation_error(self):
        successful_solver = object()
        night_result = OptimizationPhaseResult(
            name="night_count_balance",
            status="OPTIMAL",
            objective_value=0,
            optimal=True,
            solver=successful_solver,
        )
        unknown_result = OptimizationPhaseResult(
            name="day_staffing_balance",
            status="UNKNOWN",
            objective_value=None,
            optimal=False,
            solver=None,
        )

        with (
            patch.object(
                shift_optimization,
                "_solve_and_fix_objective",
                side_effect=[night_result, unknown_result],
            ) as solve_phase,
            patch.object(
                shift_optimization,
                "_add_shift_solution_hints",
            ) as add_hints,
        ):
            with self.assertRaisesRegex(
                ShiftGenerationError,
                "制限時間内に解を見つけられませんでした",
            ):
                shift_optimization._run_optimization_phases(
                    model=object(),
                    shift_vars={},
                    phase_definitions=[
                        SimpleNamespace(
                            name="night_count_balance",
                            objective=0,
                            max_time_seconds=1,
                        ),
                        SimpleNamespace(
                            name="day_staffing_balance",
                            objective=1,
                            max_time_seconds=1,
                        ),
                        SimpleNamespace(
                            name="long_streak",
                            objective=2,
                            max_time_seconds=1,
                        ),
                    ],
                )

        self.assertEqual(solve_phase.call_count, 2)
        add_hints.assert_called_once()
        self.assertIs(
            add_hints.call_args.kwargs["solver"],
            successful_solver,
        )

    def test_unknown_phase_does_not_read_or_fix_an_objective_value(self):
        model = Mock()
        solver = Mock()
        solver.Solve.return_value = cp_model.UNKNOWN
        solver.StatusName.return_value = "UNKNOWN"
        solver.WallTime.return_value = 1.0

        with patch.object(
            shift_optimization,
            "_new_solver",
            return_value=solver,
        ):
            result = shift_optimization._solve_and_fix_objective(
                model=model,
                objective="objective",
                phase_name="long_streak",
                max_time_seconds=1,
            )

        self.assertEqual(result.status, "UNKNOWN")
        self.assertIsNone(result.objective_value)
        self.assertFalse(result.optimal)
        self.assertIsNone(result.solver)
        solver.ObjectiveValue.assert_not_called()
        model.Add.assert_not_called()
        model.ClearObjective.assert_called_once_with()

    def test_generate_shift_uses_previous_solution_when_long_streak_is_unknown(self):
        self.create_rule(
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=31,
        )
        self.create_staff_member(name="途中解採用")
        solve_phase = shift_optimization._solve_and_fix_objective

        def solve_until_long_streak(**kwargs):
            if kwargs["phase_name"] == "long_streak":
                return OptimizationPhaseResult(
                    name="long_streak",
                    status="UNKNOWN",
                    objective_value=None,
                    optimal=False,
                    solver=None,
                )
            return solve_phase(**kwargs)

        with patch.object(
            shift_optimization,
            "_solve_and_fix_objective",
            side_effect=solve_until_long_streak,
        ):
            result = generate_shift(self.shift_plan)

        summary = result.optimization_summary
        self.assertEqual(result.status, "success")
        self.assertEqual(len(result.shifts), 28)
        self.assertEqual(
            summary.phase_statuses["long_streak"],
            "UNKNOWN",
        )
        self.assertFalse(summary.phase_optimal_flags["long_streak"])
        self.assertTrue(
            all(
                summary.phase_statuses[phase_name]
                in {"OPTIMAL", "FEASIBLE"}
                for phase_name in (
                    "night_count_balance",
                    "day_staffing_balance",
                )
            )
        )
        self.assertEqual(
            result.solver_status,
            summary.phase_statuses["day_staffing_balance"],
        )
        self.assertEqual(
            result.optimization_incomplete_message,
            "処理時間の上限に達したため、"
            "連勤配置の調整を完了できませんでした。"
            "夜勤回数・日勤人数・能力配置まで調整したシフトを使用しています。",
        )

    def test_empty_phase_definitions_raise_explicit_error(self):
        with self.assertRaisesRegex(
            ShiftGenerationError, "最適化フェーズが定義されていません"
        ):
            shift_optimization._run_optimization_phases(
                model=object(), shift_vars={}, phase_definitions=[]
            )

    def test_solver_time_scale_boundaries(self):
        expected_scales = [
            (0, 1.00),
            (1, 1.00),
            (20, 1.00),
            (21, 1.25),
            (30, 1.25),
            (31, 1.50),
            (40, 1.50),
            (41, 1.75),
            (100, 1.75),
        ]

        for staff_count, expected_scale in expected_scales:
            with self.subTest(staff_count=staff_count):
                self.assertAlmostEqual(
                    shift_optimization._calculate_solver_time_scale(staff_count),
                    expected_scale,
                )

    def test_phase_time_limit_rounds_up(self):
        cases = [
            (25, 1.00, 25),
            (25, 1.25, 32),
            (25, 1.50, 38),
            (25, 1.75, 44),
            (10, 1.25, 13),
            (15, 1.50, 23),
        ]

        for base_seconds, time_scale, expected_seconds in cases:
            with self.subTest(base_seconds=base_seconds, time_scale=time_scale):
                self.assertEqual(
                    shift_optimization._calculate_phase_time_limit(
                        base_seconds=base_seconds,
                        time_scale=time_scale,
                    ),
                    expected_seconds,
                )

    def test_day_staffing_balance_time_limit_scales_with_staff_count(self):
        for staff_count, expected_seconds in (
            (20, 60),
            (21, 75),
            (31, 90),
            (41, 105),
        ):
            with self.subTest(staff_count=staff_count):
                phase_definitions = shift_optimization._build_phase_definitions(
                    day_staffing_balance_data=DayStaffingBalanceData(
                        objective_score=1
                    ),
                    night_count_balance_data=SimpleNamespace(objective_score=2),
                    long_streak_terms=[4],
                    staff_count=staff_count,
                )
                day_phase = next(
                    phase
                    for phase in phase_definitions
                    if phase.name == "day_staffing_balance"
                )

                self.assertEqual(
                    day_phase.name,
                    "day_staffing_balance",
                )
                self.assertEqual(
                    day_phase.max_time_seconds,
                    expected_seconds,
                )

    def test_phase_definitions_scale_all_limits_with_staff_count(self):
        cases = (
            (20, [30, 60, 20]),
            (30, [38, 75, 25]),
            (40, [45, 90, 30]),
            (41, [53, 105, 35]),
        )

        for staff_count, expected_limits in cases:
            with self.subTest(staff_count=staff_count):
                phase_definitions = shift_optimization._build_phase_definitions(
                    day_staffing_balance_data=DayStaffingBalanceData(
                        objective_score=1
                    ),
                    night_count_balance_data=SimpleNamespace(
                        objective_score=4
                    ),
                    long_streak_terms=[7],
                    staff_count=staff_count,
                )

                self.assertEqual(
                    [
                        phase.max_time_seconds
                        for phase in phase_definitions
                    ],
                    expected_limits,
                )
                self.assertEqual(
                    [phase.name for phase in phase_definitions],
                    list(shift_optimization.PHASE_TIME_LIMITS),
                )

    def test_summary_records_every_optimization_phase_status(self):
        self.create_rule(
            required_day_staff=1, required_night_staff=0,
            off_days_per_staff=0, max_consecutive_work_days=31,
        )
        self.create_staff_member(name="フェーズ確認")

        summary = generate_shift(self.shift_plan).optimization_summary
        expected_phases = set(shift_optimization.PHASE_TIME_LIMITS)

        self.assertEqual(set(summary.phase_statuses), expected_phases)
        self.assertEqual(set(summary.phase_optimal_flags), expected_phases)
        self.assertEqual(
            list(summary.phase_statuses),
            [
                "night_count_balance",
                "day_staffing_balance",
                "long_streak",
            ],
        )
        self.assertNotIn("ability_balance", summary.phase_statuses)
        self.assertIn("day_staffing_balance", summary.phase_statuses)
        self.assertIn("night_count_balance", summary.phase_statuses)
        self.assertTrue(
            all(status in {"OPTIMAL", "FEASIBLE"} for status in summary.phase_statuses.values())
        )
        self.assertTrue(
            all(isinstance(optimal, bool) for optimal in summary.phase_optimal_flags.values())
        )

    def test_long_streak_penalty_includes_previous_month_work(self):
        self.create_rule(
            required_day_staff=0, required_night_staff=0, off_days_per_staff=26,
            max_consecutive_work_days=5,
        )
        staff_member = self.create_staff_member(name="月跨ぎ連勤")
        ShiftCarryover.objects.create(
            shift_plan=self.shift_plan,
            staff_member=staff_member,
            source=ShiftCarryover.SourceChoices.PREVIOUS_PLAN,
            previous_last_shift_type=ShiftResult.ShiftTypeChoices.DAY,
            previous_consecutive_work_days=3,
        )
        for day in (1, 2):
            DateShiftRule.objects.create(
                shift_plan=self.shift_plan,
                target_date=date(2026, 2, day),
                required_day_staff=1,
            )
            ShiftResult.objects.create(
                shift_plan=self.shift_plan, staff_member=staff_member,
                date=date(2026, 2, day), shift_type=ShiftResult.ShiftTypeChoices.DAY,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )
        for day in range(3, 29):
            ShiftResult.objects.create(
                shift_plan=self.shift_plan, staff_member=staff_member,
                date=date(2026, 2, day), shift_type=ShiftResult.ShiftTypeChoices.PAID_LEAVE,
                input_type=ShiftResult.InputTypeChoices.MANUAL,
            )

        summary = generate_shift(self.shift_plan).optimization_summary
        self.assertGreaterEqual(summary.long_streak_penalty, LONG_STREAK_WEIGHTS["at_max"])


class AbilityDistributionObjectiveTests(SimpleTestCase):
    target_date = date(2026, 2, 1)

    def solve_distribution(
        self,
        *,
        ability_levels,
        assigned_indices=None,
        assignments_by_date=None,
        eligible_indices=None,
        shift_type=ShiftResult.ShiftTypeChoices.DAY,
    ):
        staff_members = [
            SimpleNamespace(id=index + 1, ability_level=ability_level)
            for index, ability_level in enumerate(ability_levels)
        ]
        if eligible_indices is None:
            eligible_indices = range(len(staff_members))
        eligible_indices = tuple(eligible_indices)
        eligible_staff = [
            staff_members[index] for index in eligible_indices
        ]
        if assignments_by_date is None:
            assignments_by_date = {
                self.target_date: assigned_indices or [],
            }
        assignments_by_date = {
            target_date: set(indices)
            for target_date, indices in assignments_by_date.items()
        }
        model = cp_model.CpModel()
        shift_vars = {}
        for target_date, date_assignments in assignments_by_date.items():
            for index, staff_member in enumerate(staff_members):
                shift_var = model.NewBoolVar(
                    f"shift_{staff_member.id}_{target_date}_{shift_type}"
                )
                model.Add(shift_var == int(index in date_assignments))
                shift_vars[(staff_member.id, target_date)] = {
                    shift_type: shift_var,
                }

        data = shift_optimization._build_ability_distribution_objective(
            model=model,
            month_dates=list(assignments_by_date),
            shift_vars=shift_vars,
            shift_type=shift_type,
            eligible_staff=eligible_staff,
        )
        model.Minimize(data.objective_score)
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        self.assertIsInstance(data, AbilityDistributionData)
        return solver, data

    def threshold_values(self, solver, variables):
        return tuple(
            solver.Value(variables[(self.target_date, threshold)])
            for threshold in shift_optimization.ABILITY_THRESHOLDS
        )

    def build_night_integration_model(self, ability_levels, date_count):
        staff_members = [
            SimpleNamespace(
                id=index + 1,
                ability_level=ability_level,
                can_night_shift=True,
            )
            for index, ability_level in enumerate(ability_levels)
        ]
        month_dates = [
            date(2026, 2, index + 1)
            for index in range(date_count)
        ]
        model = cp_model.CpModel()
        shift_vars = {
            (staff.id, target_date): {
                ShiftResult.ShiftTypeChoices.NIGHT: model.NewBoolVar(
                    f"integrated_night_{staff.id}_{target_date}"
                )
            }
            for staff in staff_members
            for target_date in month_dates
        }
        return model, staff_members, month_dates, shift_vars

    def solve_night_integration(
        self,
        *,
        model,
        staff_members,
        month_dates,
        shift_vars,
        pattern_terms=(),
    ):
        ability_data = (
            shift_optimization._build_ability_distribution_objective(
                model=model,
                month_dates=month_dates,
                shift_vars=shift_vars,
                shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
                eligible_staff=staff_members,
            )
        )
        night_count_data = (
            shift_optimization._build_night_count_balance_objective(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                night_after_night_pattern_terms=pattern_terms,
                ability_distribution_data=ability_data,
            )
        )
        model.Minimize(night_count_data.objective_score)
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        return solver, night_count_data, ability_data

    def build_day_integration_model(self, ability_levels, required_counts):
        staff_members = [
            SimpleNamespace(
                id=index + 1,
                ability_level=ability_level,
            )
            for index, ability_level in enumerate(ability_levels)
        ]
        month_dates = [
            date(2026, 2, index + 1)
            for index in range(len(required_counts))
        ]
        model = cp_model.CpModel()
        shift_vars = {
            (staff.id, target_date): {
                ShiftResult.ShiftTypeChoices.DAY: model.NewBoolVar(
                    f"integrated_day_{staff.id}_{target_date}"
                )
            }
            for staff in staff_members
            for target_date in month_dates
        }
        effective_rules = {
            target_date: SimpleNamespace(required_day_staff=required_count)
            for target_date, required_count in zip(
                month_dates, required_counts
            )
        }
        return (
            model,
            staff_members,
            month_dates,
            shift_vars,
            effective_rules,
        )

    def solve_day_integration(
        self,
        *,
        model,
        staff_members,
        month_dates,
        shift_vars,
        effective_rules,
    ):
        ability_data = (
            shift_optimization._build_ability_distribution_objective(
                model=model,
                month_dates=month_dates,
                shift_vars=shift_vars,
                shift_type=ShiftResult.ShiftTypeChoices.DAY,
                eligible_staff=staff_members,
            )
        )
        day_staffing_data = (
            shift_optimization._build_day_staffing_balance_data(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                effective_rules=effective_rules,
                ability_distribution_data=ability_data,
            )
        )
        model.Minimize(day_staffing_data.objective_score)
        solver = cp_model.CpSolver()
        self.assertEqual(solver.Solve(model), cp_model.OPTIMAL)
        return solver, day_staffing_data, ability_data

    def test_counts_staff_at_or_above_each_ability_threshold(self):
        solver, data = self.solve_distribution(
            ability_levels=[5, 4, 2],
            assigned_indices=[0, 1, 2],
        )

        self.assertEqual(
            data.thresholds,
            shift_optimization.ABILITY_THRESHOLDS,
        )
        self.assertEqual(
            self.threshold_values(solver, data.threshold_count_vars),
            (2, 2, 1),
        )

    def test_distinguishes_distributions_with_the_same_ability_total(self):
        ability_levels = [5, 1, 3, 3, 3]
        first_solver, first_data = self.solve_distribution(
            ability_levels=ability_levels,
            assigned_indices=[0, 1],
        )
        second_solver, second_data = self.solve_distribution(
            ability_levels=ability_levels,
            assigned_indices=[2, 3],
        )

        self.assertEqual(sum(ability_levels[index] for index in (0, 1)), 6)
        self.assertEqual(sum(ability_levels[index] for index in (2, 3)), 6)
        self.assertEqual(
            self.threshold_values(
                first_solver, first_data.threshold_count_vars
            ),
            (1, 1, 1),
        )
        self.assertEqual(
            self.threshold_values(
                second_solver, second_data.threshold_count_vars
            ),
            (2, 0, 0),
        )
        self.assertNotEqual(
            first_solver.Value(first_data.objective_score),
            second_solver.Value(second_data.objective_score),
        )

    def test_proportional_distribution_has_zero_deviation(self):
        solver, data = self.solve_distribution(
            ability_levels=[4] * 5 + [2] * 5,
            assigned_indices=[0, 5],
        )

        self.assertEqual(
            solver.Value(data.deviation_vars[(self.target_date, 4)]),
            0,
        )
        self.assertEqual(solver.Value(data.max_deviation), 0)
        self.assertEqual(solver.Value(data.total_deviation), 0)
        self.assertEqual(solver.Value(data.objective_score), 0)

    def test_fractional_ideal_uses_equal_integer_deviations(self):
        first_solver, first_data = self.solve_distribution(
            ability_levels=[4] * 5 + [2] * 5,
            assigned_indices=[0, 5, 6],
        )
        second_solver, second_data = self.solve_distribution(
            ability_levels=[4] * 5 + [2] * 5,
            assigned_indices=[0, 1, 5],
        )

        first_deviation = first_solver.Value(
            first_data.deviation_vars[(self.target_date, 4)]
        )
        second_deviation = second_solver.Value(
            second_data.deviation_vars[(self.target_date, 4)]
        )
        self.assertEqual(first_deviation, 5)
        self.assertEqual(second_deviation, 5)
        self.assertEqual(
            first_solver.Value(first_data.objective_score),
            second_solver.Value(second_data.objective_score),
        )

    def test_penalty_increases_as_distribution_moves_from_ideal(self):
        assignments = (
            [0, 1, 5, 6],
            [0, 5, 6, 7],
            [5, 6, 7, 8],
        )
        deviations = []
        objective_scores = []
        for assigned_indices in assignments:
            solver, data = self.solve_distribution(
                ability_levels=[4] * 5 + [2] * 5,
                assigned_indices=assigned_indices,
            )
            deviations.append(
                solver.Value(
                    data.deviation_vars[(self.target_date, 4)]
                )
            )
            objective_scores.append(solver.Value(data.objective_score))

        self.assertEqual(deviations, [0, 10, 20])
        self.assertLess(objective_scores[0], objective_scores[1])
        self.assertLess(objective_scores[1], objective_scores[2])

    def test_actual_shift_count_changes_the_proportional_target(self):
        first_solver, first_data = self.solve_distribution(
            ability_levels=[4] * 5 + [2] * 5,
            assigned_indices=[0, 5, 6],
        )
        second_solver, second_data = self.solve_distribution(
            ability_levels=[4] * 5 + [2] * 5,
            assigned_indices=[0, 5, 6, 7, 8, 9],
        )

        self.assertEqual(
            first_solver.Value(
                first_data.actual_shift_count_vars[self.target_date]
            ),
            3,
        )
        self.assertEqual(
            second_solver.Value(
                second_data.actual_shift_count_vars[self.target_date]
            ),
            6,
        )
        self.assertEqual(
            first_solver.Value(
                first_data.threshold_count_vars[(self.target_date, 4)]
            ),
            1,
        )
        self.assertEqual(
            second_solver.Value(
                second_data.threshold_count_vars[(self.target_date, 4)]
            ),
            1,
        )
        self.assertEqual(
            first_solver.Value(
                first_data.deviation_vars[(self.target_date, 4)]
            ),
            5,
        )
        self.assertEqual(
            second_solver.Value(
                second_data.deviation_vars[(self.target_date, 4)]
            ),
            20,
        )

    def test_aggregates_every_date_and_threshold_lexicographically(self):
        second_date = date(2026, 2, 2)
        solver, data = self.solve_distribution(
            ability_levels=[5, 4, 3, 2, 1],
            assignments_by_date={
                self.target_date: [0, 3],
                second_date: [0, 1, 4],
            },
        )

        expected_deviations = {
            (self.target_date, 3): 1,
            (self.target_date, 4): 1,
            (self.target_date, 5): 3,
            (second_date, 3): 1,
            (second_date, 4): 4,
            (second_date, 5): 2,
        }
        self.assertEqual(
            {
                key: solver.Value(variable)
                for key, variable in data.deviation_vars.items()
            },
            expected_deviations,
        )
        self.assertEqual(solver.Value(data.max_deviation), 4)
        self.assertEqual(solver.Value(data.total_deviation), 12)
        self.assertEqual(solver.Value(data.objective_score), 144)

    def test_eligible_population_can_be_changed_by_the_caller(self):
        all_solver, all_data = self.solve_distribution(
            ability_levels=[5, 1, 1, 1],
            assigned_indices=[0, 1],
            eligible_indices=[0, 1, 2, 3],
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        )
        subset_solver, subset_data = self.solve_distribution(
            ability_levels=[5, 1, 1, 1],
            assigned_indices=[0, 1],
            eligible_indices=[0, 1],
            shift_type=ShiftResult.ShiftTypeChoices.DAY,
        )

        self.assertEqual(all_data.eligible_staff_count, 4)
        self.assertEqual(subset_data.eligible_staff_count, 2)
        self.assertEqual(all_data.eligible_above_counts[4], 1)
        self.assertEqual(subset_data.eligible_above_counts[4], 1)
        self.assertEqual(
            all_solver.Value(
                all_data.actual_shift_count_vars[self.target_date]
            ),
            2,
        )
        self.assertEqual(
            subset_solver.Value(
                subset_data.actual_shift_count_vars[self.target_date]
            ),
            2,
        )
        self.assertEqual(
            all_solver.Value(
                all_data.deviation_vars[(self.target_date, 4)]
            ),
            2,
        )
        self.assertEqual(
            subset_solver.Value(
                subset_data.deviation_vars[(self.target_date, 4)]
            ),
            0,
        )

    def test_builder_uses_the_supplied_shift_type(self):
        solver, data = self.solve_distribution(
            ability_levels=[5, 2, 1],
            assigned_indices=[0, 1],
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )

        self.assertEqual(data.shift_type, ShiftResult.ShiftTypeChoices.NIGHT)
        self.assertEqual(
            solver.Value(data.actual_shift_count_vars[self.target_date]),
            2,
        )
        self.assertEqual(
            self.threshold_values(solver, data.threshold_count_vars),
            (1, 1, 1),
        )
        self.assertEqual(
            self.threshold_values(solver, data.deviation_vars),
            (1, 1, 1),
        )

    def test_empty_eligible_population_has_zero_distribution(self):
        solver, data = self.solve_distribution(
            ability_levels=[5],
            assigned_indices=[],
            eligible_indices=[],
            shift_type=ShiftResult.ShiftTypeChoices.NIGHT,
        )

        self.assertEqual(data.shift_type, ShiftResult.ShiftTypeChoices.NIGHT)
        self.assertEqual(data.eligible_staff_count, 0)
        self.assertEqual(
            solver.Value(data.actual_shift_count_vars[self.target_date]),
            0,
        )
        self.assertEqual(
            self.threshold_values(solver, data.threshold_count_vars),
            (0, 0, 0),
        )
        self.assertEqual(solver.Value(data.max_deviation), 0)
        self.assertEqual(solver.Value(data.total_deviation), 0)
        self.assertEqual(solver.Value(data.objective_score), 0)

    def test_day_staffing_balance_has_priority_over_ability_distribution(self):
        (
            model,
            staff_members,
            month_dates,
            shift_vars,
            effective_rules,
        ) = self.build_day_integration_model(
            ability_levels=[5, 4, 1, 1],
            required_counts=[2, 2],
        )
        choice = model.NewBoolVar("prefer_ability_over_day_staffing")
        assignments = (
            (1, 1 - choice, choice, 0),
            (0, choice, 1, 1),
        )
        for target_date, date_assignments in zip(
            month_dates, assignments
        ):
            for staff, assignment in zip(
                staff_members, date_assignments
            ):
                model.Add(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.DAY
                    ]
                    == assignment
                )

        solver, day_staffing_data, ability_data = (
            self.solve_day_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                effective_rules=effective_rules,
            )
        )

        self.assertEqual(solver.Value(choice), 0)
        self.assertEqual(solver.Value(day_staffing_data.delta_range), 0)
        self.assertEqual(solver.Value(ability_data.max_deviation), 4)
        self.assertEqual(solver.Value(ability_data.total_deviation), 20)
        self.assertEqual(solver.Value(day_staffing_data.objective_score), 112)

    def test_day_integration_spreads_ability_when_staffing_is_equal(self):
        (
            model,
            staff_members,
            month_dates,
            shift_vars,
            effective_rules,
        ) = self.build_day_integration_model(
            ability_levels=[5, 4, 1, 1],
            required_counts=[2, 2],
        )
        choice = model.NewBoolVar("spread_day_ability")
        assignments = (
            (1, 1 - choice, choice, 0),
            (0, choice, 1 - choice, 1),
        )
        for target_date, date_assignments in zip(
            month_dates, assignments
        ):
            for staff, assignment in zip(
                staff_members, date_assignments
            ):
                model.Add(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.DAY
                    ]
                    == assignment
                )

        solver, day_staffing_data, ability_data = (
            self.solve_day_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                effective_rules=effective_rules,
            )
        )

        self.assertEqual(solver.Value(choice), 1)
        self.assertEqual(solver.Value(day_staffing_data.delta_range), 0)
        self.assertEqual(
            [
                solver.Value(
                    ability_data.threshold_count_vars[(target_date, 3)]
                )
                for target_date in month_dates
            ],
            [1, 1],
        )
        self.assertEqual(
            [
                solver.Value(
                    ability_data.threshold_count_vars[(target_date, 4)]
                )
                for target_date in month_dates
            ],
            [1, 1],
        )
        self.assertEqual(
            sorted(
                solver.Value(
                    ability_data.threshold_count_vars[(target_date, 5)]
                )
                for target_date in month_dates
            ),
            [0, 1],
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 2)
        self.assertEqual(solver.Value(ability_data.total_deviation), 4)
        self.assertEqual(solver.Value(day_staffing_data.objective_score), 50)

    def test_day_integration_spreads_every_ability_threshold(self):
        (
            model,
            staff_members,
            month_dates,
            shift_vars,
            effective_rules,
        ) = self.build_day_integration_model(
            ability_levels=[5, 5, 4, 4, 3, 3, 1, 1],
            required_counts=[4, 4],
        )
        for target_date in month_dates:
            model.Add(
                sum(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.DAY
                    ]
                    for staff in staff_members
                )
                == 4
            )
        for staff in staff_members:
            model.Add(
                sum(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.DAY
                    ]
                    for target_date in month_dates
                )
                == 1
            )

        solver, day_staffing_data, ability_data = (
            self.solve_day_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                effective_rules=effective_rules,
            )
        )

        for target_date in month_dates:
            self.assertEqual(
                tuple(
                    solver.Value(
                        ability_data.threshold_count_vars[
                            (target_date, threshold)
                        ]
                    )
                    for threshold in shift_optimization.ABILITY_THRESHOLDS
                ),
                (3, 2, 1),
            )
        self.assertEqual(solver.Value(day_staffing_data.delta_range), 0)
        self.assertEqual(solver.Value(ability_data.max_deviation), 0)
        self.assertEqual(solver.Value(ability_data.total_deviation), 0)

    def test_night_integration_spreads_every_ability_threshold(self):
        model, staff_members, month_dates, shift_vars = (
            self.build_night_integration_model(
                ability_levels=[5, 5, 4, 4, 3, 3, 1, 1],
                date_count=2,
            )
        )
        for target_date in month_dates:
            model.Add(
                sum(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.NIGHT
                    ]
                    for staff in staff_members
                )
                == 4
            )
        for staff in staff_members:
            model.Add(
                sum(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.NIGHT
                    ]
                    for target_date in month_dates
                )
                == 1
            )

        solver, night_count_data, ability_data = (
            self.solve_night_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
            )
        )

        for target_date in month_dates:
            self.assertEqual(
                tuple(
                    solver.Value(
                        ability_data.threshold_count_vars[
                            (target_date, threshold)
                        ]
                    )
                    for threshold in shift_optimization.ABILITY_THRESHOLDS
                ),
                (3, 2, 1),
            )
        self.assertEqual(
            solver.Value(night_count_data.night_balance_violation),
            0,
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 0)
        self.assertEqual(solver.Value(ability_data.total_deviation), 0)

    def test_night_count_balance_has_priority_over_ability_distribution(self):
        model, staff_members, month_dates, shift_vars = (
            self.build_night_integration_model(
                ability_levels=[5, 4, 1, 1],
                date_count=2,
            )
        )
        choice = model.NewBoolVar("prefer_ability_over_night_count")
        assignments = (
            (1, 1 - choice, choice, 0),
            (choice, 0, 1 - choice, 1),
        )
        for target_date, date_assignments in zip(
            month_dates, assignments
        ):
            for staff, assignment in zip(
                staff_members, date_assignments
            ):
                model.Add(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.NIGHT
                    ]
                    == assignment
                )

        solver, night_count_data, ability_data = (
            self.solve_night_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
            )
        )

        self.assertEqual(solver.Value(choice), 0)
        self.assertEqual(
            solver.Value(night_count_data.night_balance_violation),
            0,
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 4)
        self.assertEqual(solver.Value(night_count_data.objective_score), 112)

    def test_night_pattern_penalty_has_priority_over_ability_distribution(self):
        model, staff_members, month_dates, shift_vars = (
            self.build_night_integration_model(
                ability_levels=[5, 4, 1, 1],
                date_count=1,
            )
        )
        choice = model.NewBoolVar("prefer_ability_over_night_pattern")
        for staff, assignment in zip(
            staff_members,
            (1, 1 - choice, choice, 0),
        ):
            model.Add(
                shift_vars[(staff.id, month_dates[0])][
                    ShiftResult.ShiftTypeChoices.NIGHT
                ]
                == assignment
            )

        solver, night_count_data, ability_data = (
            self.solve_night_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
                pattern_terms=[choice],
            )
        )

        self.assertEqual(solver.Value(choice), 0)
        self.assertEqual(
            solver.Value(night_count_data.night_balance_violation),
            0,
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 4)
        self.assertEqual(solver.Value(night_count_data.objective_score), 58)

    def test_night_ability_max_deviation_has_priority_over_total(self):
        model, staff_members, month_dates, shift_vars = (
            self.build_night_integration_model(
                ability_levels=[3, 3, 1, 1, 1],
                date_count=2,
            )
        )
        choice = model.NewBoolVar("prefer_total_over_max_deviation")
        assignments = (
            (choice, choice, 1 - choice, 1 - choice, 0),
            (choice, 0, 1, 1 - choice, 0),
        )
        for target_date, date_assignments in zip(
            month_dates, assignments
        ):
            for staff, assignment in zip(
                staff_members, date_assignments
            ):
                model.Add(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.NIGHT
                    ]
                    == assignment
                )

        solver, night_count_data, ability_data = (
            self.solve_night_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
            )
        )

        self.assertEqual(solver.Value(choice), 0)
        self.assertEqual(
            solver.Value(night_count_data.night_balance_violation),
            1,
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 4)
        self.assertEqual(solver.Value(ability_data.total_deviation), 8)
        self.assertEqual(solver.Value(night_count_data.objective_score), 606)

    def test_night_ability_total_deviation_breaks_equal_max_ties(self):
        model, staff_members, month_dates, shift_vars = (
            self.build_night_integration_model(
                ability_levels=[3, 1, 1, 1, 1],
                date_count=2,
            )
        )
        choice = model.NewBoolVar("prefer_larger_total_deviation")
        assignments = (
            (1, 1, 0, 0, 0),
            (choice, 1, 1 - choice, 0, 0),
        )
        for target_date, date_assignments in zip(
            month_dates, assignments
        ):
            for staff, assignment in zip(
                staff_members, date_assignments
            ):
                model.Add(
                    shift_vars[(staff.id, target_date)][
                        ShiftResult.ShiftTypeChoices.NIGHT
                    ]
                    == assignment
                )

        solver, night_count_data, ability_data = (
            self.solve_night_integration(
                model=model,
                staff_members=staff_members,
                month_dates=month_dates,
                shift_vars=shift_vars,
            )
        )

        self.assertEqual(solver.Value(choice), 0)
        self.assertEqual(
            solver.Value(night_count_data.night_balance_violation),
            1,
        )
        self.assertEqual(solver.Value(ability_data.max_deviation), 3)
        self.assertEqual(solver.Value(ability_data.total_deviation), 5)
        self.assertEqual(solver.Value(night_count_data.objective_score), 302)

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

    def test_generate_action_shows_success_message(self):
        self.create_rule(max_consecutive_work_days=31)

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "generate"},
            follow=True,
        )

        self.assertContains(response, "シフトを生成しました。")
        self.assertNotContains(response, "処理時間の上限に達したため、")

    def test_generate_action_shows_day_staffing_adjustment_as_info(self):
        self.create_rule()
        adjustment_message = (
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "日勤数：5〜6人で最適化を行なっています。"
        )
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            day_staffing_adjustment_message=adjustment_message,
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

        self.assertContains(response, "シフトを生成しました。")
        self.assertContains(response, adjustment_message, count=1)
        self.assertContains(response, "alert-info", count=1)
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )

    def test_generate_action_shows_adjustment_and_incomplete_optimization(self):
        self.create_rule()
        adjustment_message = (
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "日勤数：5〜6人で最適化を行なっています。"
        )
        incomplete_message = (
            "処理時間の上限に達したため、"
            "能力配置の均等化を完了できませんでした。"
            "それ以前の条件を反映したシフトを使用しています。"
        )
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            day_staffing_adjustment_message=adjustment_message,
            optimization_incomplete_message=incomplete_message,
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

        self.assertContains(response, "シフトを生成しました。")
        self.assertContains(response, adjustment_message, count=1)
        self.assertContains(response, incomplete_message, count=1)
        self.assertContains(response, "alert-info", count=1)
        self.assertContains(response, "alert-warning", count=1)
        self.assertNotContains(response, "シフトを生成できませんでした。")

    def test_generate_action_replaces_daily_shortages_with_one_info(self):
        self.create_rule(
            required_day_staff=3,
            max_consecutive_work_days=31,
        )
        adjustment_message = (
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "日勤数：1人で最適化を行なっています。"
        )

        response = self.client.post(
            reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
            {"action": "generate"},
            follow=True,
        )

        self.assertContains(response, "シフトを生成しました。")
        self.assertContains(response, adjustment_message, count=1)
        self.assertNotContains(response, "日勤が2人不足しています。")
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )

    def test_generate_action_shows_warning_message_when_violations_exist(self):
        self.create_rule()
        adjustment_message = (
            "設定した必要日勤数ではシフト最適化ができなかったため、"
            "日勤数：5〜7人で最適化を行なっています。"
        )
        warning_message = (
            "固定勤務や勤務条件の影響により、日勤人数を均等に配置できませんでした。"
            "可能な範囲で均等化しています。"
        )
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            day_staffing_adjustment_message=adjustment_message,
            violations=[
                ShiftGenerationViolation(
                    violation_type=(
                        ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE
                    ),
                    message=warning_message,
                )
            ],
        )

        with patch("shifts.views.generate_and_save_shift", return_value=fake_result):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        self.assertContains(response, "シフトを生成しました。")
        self.assertContains(response, adjustment_message)
        self.assertContains(response, warning_message)
        self.assertNotContains(
            response,
            "シフトを生成しましたが、一部の条件を満たせませんでした。",
        )
        self.assertNotContains(response, "日勤が1人不足しています。")

    def test_generate_action_shows_incomplete_and_staffing_warnings_together(self):
        self.create_rule()
        incomplete_message = (
            "処理時間の上限に達したため、"
            "夜勤回数・能力配置の均等化を完了できませんでした。"
            "それ以前の条件を反映したシフトを使用しています。"
        )
        day_warning_message = (
            "固定勤務や勤務条件の影響により、日勤人数を均等に配置できませんでした。"
            "可能な範囲で均等化しています。"
        )
        night_warning_message = (
            "スタッフ間の夜勤回数差が2回あります。"
            "目標は1回以内ですが、固定勤務などの影響により調整できませんでした。"
        )
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            optimization_incomplete_message=incomplete_message,
            violations=[
                ShiftGenerationViolation(
                    violation_type=(
                        ShiftGenerationViolationType.DAY_STAFFING_IMBALANCE
                    ),
                    message=day_warning_message,
                ),
                ShiftGenerationViolation(
                    violation_type=(
                        ShiftGenerationViolationType.NIGHT_COUNT_IMBALANCE
                    ),
                    message=night_warning_message,
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

        self.assertContains(response, "シフトを生成しました。")
        self.assertContains(response, incomplete_message, count=1)
        self.assertContains(response, day_warning_message, count=1)
        self.assertContains(response, night_warning_message, count=1)
        self.assertContains(response, "alert-warning", count=2)
        self.assertNotContains(response, "シフトを生成できませんでした。")

    def test_generate_action_shows_error_message_when_generation_fails(self):
        self.create_rule()

        with patch("shifts.views.generate_and_save_shift", side_effect=ShiftGenerationError("固定条件が競合しています。")):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "シフトを生成できませんでした。")
        self.assertContains(response, "固定条件が競合しています。")

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
        self.assertContains(response, "シフトを生成しました。")
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
