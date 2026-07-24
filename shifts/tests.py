from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from staff.models import StaffMember, StaffRegularDayOff

from .forms import ShiftPlanCreateForm, ShiftRuleForm
from .shift_generator import (
    LONG_STREAK_WEIGHTS,
    ShiftGenerationError,
    ShiftGenerationResult,
    ShiftGenerationViolation,
    ShiftGenerationViolationType,
    _build_night_count_imbalance_violation,
    generate_and_save_shift,
    generate_shift,
)
from .models import DateShiftRule, DayOffRequest, ShiftCarryover, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule
from .services import (
    build_shift_carryovers,
    calculate_previous_consecutive_work_days,
    get_effective_rule_for_date,
    get_japanese_holiday_dates,
    get_month_dates,
    get_usable_previous_shift_plan,
    sync_month_boundary_assignments,
)
from .views import build_day_headers, build_shift_plan_grid


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
        self.assertContains(response, "シフトを生成中です")
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
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=0)
        staff_member = self.create_staff_member()

        result = generate_shift(self.shift_plan)
        shift_map = self.build_shift_map(result.shifts)

        self.assertFalse(
            any(
                violation.violation_type in {
                    ShiftGenerationViolationType.DAY_SHORTAGE,
                    ShiftGenerationViolationType.DAY_EXCESS,
                }
                for violation in result.violations
            )
        )
        self.assertTrue(
            all(
                shift_map[(staff_member.id, date(2026, 7, day))]
                == ShiftResult.ShiftTypeChoices.DAY
                for day in range(1, 32)
            )
        )

    def test_generate_shift_returns_day_shortage_violation_when_requirement_is_unreachable(self):
        self.create_rule(required_day_staff=2, required_night_staff=0, off_days_per_staff=0)
        self.create_staff_member()

        result = generate_shift(self.shift_plan)

        self.assertTrue(result.has_violations)
        self.assertTrue(
            any(
                violation.violation_type == ShiftGenerationViolationType.DAY_SHORTAGE
                and violation.amount == 1
                for violation in result.violations
            )
        )

    def test_generate_shift_rejects_unreachable_hard_night_requirement(self):
        self.create_rule(
            required_day_staff=0,
            required_night_staff=1,
            off_days_per_staff=0,
            night_shift_next_day_off=False,
        )
        self.create_staff_member()

        with self.assertRaises(ShiftGenerationError):
            generate_shift(self.shift_plan)

    def test_generate_shift_does_not_warn_for_day_excess(self):
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=0)
        self.create_staff_member()
        WeekdayShiftRule.objects.create(
            shift_plan=self.shift_plan,
            day_of_week=date(2026, 7, 6).weekday(),
            required_day_staff=0,
        )

        result = generate_shift(self.shift_plan)

        self.assertFalse(
            any(
                violation.violation_type == ShiftGenerationViolationType.DAY_EXCESS
                for violation in result.violations
            )
        )

    def test_generate_shift_applies_date_rule_before_weekday_rule_for_day_requirement(self):
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=0)
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

        self.assertFalse(
            any(
                violation.violation_type == ShiftGenerationViolationType.DAY_EXCESS
                and violation.date == date(2026, 7, 6)
                for violation in result.violations
            )
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

    def test_generate_shift_returns_max_consecutive_work_violation(self):
        self.create_rule(required_day_staff=1, required_night_staff=0, off_days_per_staff=0)
        self.create_staff_member()

        result = generate_shift(self.shift_plan)

        self.assertTrue(
            any(
                violation.violation_type == ShiftGenerationViolationType.MAX_CONSECUTIVE_WORK
                and violation.start_date == date(2026, 7, 1)
                and violation.end_date == date(2026, 7, 31)
                for violation in result.violations
            )
        )

    def test_training_is_counted_as_work_for_consecutive_violation(self):
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

        result = generate_shift(self.shift_plan)

        self.assertTrue(
            any(
                violation.violation_type == ShiftGenerationViolationType.MAX_CONSECUTIVE_WORK
                and violation.start_date == date(2026, 7, 7)
                and violation.end_date == date(2026, 7, 12)
                for violation in result.violations
            )
        )

    def test_special_leave_breaks_consecutive_work_violation(self):
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

        self.assertFalse(
            any(
                violation.violation_type == ShiftGenerationViolationType.MAX_CONSECUTIVE_WORK
                for violation in result.violations
            )
        )


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

    def test_night_counts_are_balanced_and_hard_requirement_remains_exact(self):
        self.create_rule(
            required_day_staff=0, required_night_staff=1, off_days_per_staff=0,
            max_consecutive_work_days=31, night_shift_next_day_off=False,
        )
        self.create_staff_member(name="夜勤A")
        self.create_staff_member(name="夜勤B")

        result = generate_shift(self.shift_plan)
        summary = result.optimization_summary
        self.assertIsNotNone(summary)
        self.assertLessEqual(summary.night_shift_count_max - summary.night_shift_count_min, 1)
        for target_date in get_month_dates(2026, 2):
            self.assertEqual(sum(
                shift.date == target_date and shift.shift_type == ShiftResult.ShiftTypeChoices.NIGHT
                for shift in result.shifts
            ), 1)

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

    def test_day_counts_are_balanced_without_worsening_semi_hard_score(self):
        self.create_rule(
            required_day_staff=1, required_night_staff=0, off_days_per_staff=14,
            max_consecutive_work_days=31,
        )
        self.create_staff_member(name="日勤A")
        self.create_staff_member(name="日勤B")

        summary = generate_shift(self.shift_plan).optimization_summary
        self.assertEqual(summary.semi_hard_score, 0)
        self.assertEqual(summary.day_shift_count_max - summary.day_shift_count_min, 0)
        self.assertTrue(summary.semi_hard_optimal)

    def test_ability_balance_uses_integer_average_equivalent(self):
        self.create_rule(
            required_day_staff=2, required_night_staff=0, off_days_per_staff=14,
            max_consecutive_work_days=31,
        )
        for level in (1, 2, 4, 5):
            staff_member = self.create_staff_member(name=f"能力{level}")
            staff_member.ability_level = level
            staff_member.save(update_fields=["ability_level"])

        summary = generate_shift(self.shift_plan).optimization_summary
        self.assertEqual(summary.semi_hard_score, 0)
        self.assertEqual(summary.ability_balance_penalty, 0)
        self.assertGreaterEqual(summary.soft_score, 0)

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
        self.assertEqual(summary.semi_hard_score, 0)
        self.assertGreaterEqual(summary.long_streak_penalty, LONG_STREAK_WEIGHTS["at_max"])


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
            "max_consecutive_work_days": 5,
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

        with patch("shifts.shift_generator.ShiftResult.objects.bulk_create", side_effect=RuntimeError("boom")):
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

    def test_generate_action_shows_warning_message_when_violations_exist(self):
        self.create_rule()
        fake_result = ShiftGenerationResult(
            status="success",
            shifts=[],
            violations=[
                ShiftGenerationViolation(
                    violation_type=ShiftGenerationViolationType.DAY_SHORTAGE,
                    message="8月10日の日勤が1人不足しています。",
                )
            ],
        )

        with patch("shifts.views.generate_and_save_shift", return_value=fake_result):
            response = self.client.post(
                reverse("shifts:edit", kwargs={"pk": self.shift_plan.pk}),
                {"action": "generate"},
                follow=True,
            )

        self.assertContains(response, "シフトを生成しましたが、一部の条件を満たせませんでした。")
        self.assertContains(response, "8月10日の日勤が1人不足しています。")

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
