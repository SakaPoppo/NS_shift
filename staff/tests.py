from django.contrib.auth import get_user_model
from django.test import TestCase

from .forms import StaffMemberForm
from .models import StaffMember


class StaffMemberModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staff-user",
            password="password123",
        )

    def test_ability_level_defaults_to_two(self):
        staff_member = StaffMember.objects.create(
            user=self.user,
            name="山田 花子",
        )

        self.assertEqual(staff_member.ability_level, StaffMember.AbilityLevelChoices.LEVEL_2)

    def test_ability_levels_one_to_five_can_be_saved(self):
        for level in range(1, 6):
            with self.subTest(level=level):
                staff_member = StaffMember.objects.create(
                    user=self.user,
                    name=f"スタッフ{level}",
                    gender=StaffMember.GenderChoices.FEMALE,
                    job=StaffMember.JobChoices.NURSE,
                    role=StaffMember.RoleChoices.MEMBER,
                    ability_level=level,
                )

                self.assertEqual(staff_member.ability_level, level)


class StaffMemberFormTests(TestCase):
    def test_form_accepts_valid_ability_level(self):
        form = StaffMemberForm(
            data={
                "name": "佐藤 花子",
                "gender": StaffMember.GenderChoices.FEMALE,
                "job": StaffMember.JobChoices.NURSE,
                "role": StaffMember.RoleChoices.LEADER,
                "ability_level": StaffMember.AbilityLevelChoices.LEVEL_4,
                "can_night_shift": "True",
                "regular_days_off": [0, 2],
            }
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_invalid_ability_level(self):
        form = StaffMemberForm(
            data={
                "name": "佐藤 花子",
                "gender": StaffMember.GenderChoices.FEMALE,
                "job": StaffMember.JobChoices.NURSE,
                "role": StaffMember.RoleChoices.LEADER,
                "ability_level": 9,
                "can_night_shift": "True",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("ability_level", form.errors)
