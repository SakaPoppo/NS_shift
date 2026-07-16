from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from staff.models import StaffMember

from .forms import ShiftRuleForm
from .models import DateShiftRule, ShiftPlan, ShiftResult, ShiftRule, WeekdayShiftRule
from .services import get_effective_rule_for_date
from .views import build_shift_plan_grid


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
            title="2026年7月 シフト表",
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
            (self.staff_member.id, month_dates[6]),
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
            title="2026年8月 シフト表",
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
            title="2026年8月 シフト表",
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
            title="2026年8月 ICUシフト表",
        )
        self.other_shift_plan = ShiftPlan.objects.create(
            user=self.other_user,
            year=2026,
            month=9,
            title="他ユーザーのシフト表",
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
                "title": "2026年10月 シフト表",
                "year": "2026",
                "month": "10",
            },
        )

        created_shift_plan = ShiftPlan.objects.get(user=self.user, year=2026, month=10)
        self.assertRedirects(response, reverse("shifts:conditions", kwargs={"pk": created_shift_plan.pk}))

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
