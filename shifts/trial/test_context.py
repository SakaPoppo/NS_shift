from collections import Counter
from datetime import date

from django.test import SimpleTestCase

from shifts.models import ShiftResult
from shifts.shift_generation.payload import build_optimizer_payload

from .context import (
    build_trial_generation_context,
    get_trial_off_days,
    get_trial_target_year_month,
)
from .data import TRIAL_STAFF_MEMBERS


class TrialGenerationContextTests(SimpleTestCase):
    def test_target_month_is_the_month_after_the_reference_date(self):
        self.assertEqual(get_trial_target_year_month(date(2026, 10, 6)), (2026, 11))
        self.assertEqual(get_trial_target_year_month(date(2026, 12, 10)), (2027, 1))

    def test_sample_staff_matches_the_fixed_composition(self):
        self.assertEqual(len(TRIAL_STAFF_MEMBERS), 20)
        self.assertEqual({staff.id for staff in TRIAL_STAFF_MEMBERS}, set(range(1, 21)))
        self.assertEqual(
            Counter(staff.ability_level for staff in TRIAL_STAFF_MEMBERS),
            {5: 2, 4: 4, 3: 5, 2: 5, 1: 4},
        )
        self.assertEqual(
            sum(staff.role == "leader" for staff in TRIAL_STAFF_MEMBERS), 9
        )
        self.assertTrue(all(staff.can_night_shift for staff in TRIAL_STAFF_MEMBERS))
        staff_with_regular_day_off = [
            staff for staff in TRIAL_STAFF_MEMBERS if staff.regular_days_off
        ]
        self.assertEqual(len(staff_with_regular_day_off), 3)
        self.assertTrue(
            all(len(staff.regular_days_off) <= 1 for staff in TRIAL_STAFF_MEMBERS)
        )

    def test_trial_context_uses_fixed_rules_and_monthly_off_days(self):
        context = build_trial_generation_context(date(2026, 1, 15))
        rule = context.shift_rule

        self.assertEqual(rule.required_day_staff, 7)
        self.assertEqual(rule.required_night_staff, 3)
        self.assertEqual(rule.required_leader_staff, 1)
        self.assertEqual(rule.max_consecutive_work_days, 5)
        self.assertTrue(rule.night_shift_next_day_off)
        self.assertEqual(rule.off_days_per_staff, 9)
        self.assertEqual(set(context.effective_off_days.values()), {9})

    def test_trial_off_days_depend_on_the_number_of_days_in_the_month(self):
        self.assertEqual(get_trial_off_days(2027, 2), 9)
        self.assertEqual(get_trial_off_days(2028, 2), 10)
        self.assertEqual(get_trial_off_days(2026, 4), 10)
        self.assertEqual(get_trial_off_days(2026, 5), 10)

    def test_trial_context_includes_month_start_fixed_assignments(self):
        context = build_trial_generation_context(date(2026, 10, 6))
        first_date, second_date = context.month_dates[:2]

        for staff_id in (2, 9, 15):
            self.assertEqual(
                context.fixed_assignments[(staff_id, first_date)],
                ShiftResult.ShiftTypeChoices.AFTER_NIGHT,
            )
            self.assertEqual(
                context.fixed_assignments[(staff_id, second_date)],
                ShiftResult.ShiftTypeChoices.OFF,
            )
        for staff_id in (4, 11, 17):
            self.assertEqual(
                context.fixed_assignments[(staff_id, first_date)],
                ShiftResult.ShiftTypeChoices.OFF,
            )

    def test_trial_context_expands_regular_days_off_into_fixed_assignments(self):
        context = build_trial_generation_context(date(2026, 10, 6))

        for staff in TRIAL_STAFF_MEMBERS:
            for target_date in context.month_dates:
                if target_date.weekday() in staff.regular_days_off:
                    self.assertEqual(
                        context.fixed_assignments[(staff.id, target_date)],
                        ShiftResult.ShiftTypeChoices.OFF,
                    )

    def test_trial_context_is_database_independent_and_uses_shared_payload(self):
        context = build_trial_generation_context(date(2026, 10, 6))
        payload = build_optimizer_payload(context)

        self.assertEqual(len(context.staff_members), 20)
        self.assertEqual(
            context.previous_consecutive_work_days,
            {staff.id: 0 for staff in TRIAL_STAFF_MEMBERS},
        )
        self.assertEqual(len(payload["staff_members"]), 20)
        self.assertEqual(payload["staff_members"][17]["regular_days_off"], [2])
