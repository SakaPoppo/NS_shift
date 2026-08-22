from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

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

    def test_holiday_off_defaults_to_false(self):
        staff_member = StaffMember.objects.create(user=self.user, name="祝日設定なし")
        self.assertFalse(staff_member.is_holiday_off)

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

    def test_form_saves_and_restores_holiday_off(self):
        user = get_user_model().objects.create_user(username="holiday-form-user", password="x")
        form = StaffMemberForm(data={
            "name": "祝日休みスタッフ",
            "gender": StaffMember.GenderChoices.FEMALE,
            "job": StaffMember.JobChoices.NURSE,
            "role": StaffMember.RoleChoices.MEMBER,
            "ability_level": StaffMember.AbilityLevelChoices.LEVEL_2,
            "can_night_shift": "True",
            "is_holiday_off": "on",
            "regular_days_off": [0, 2],
        })
        self.assertTrue(form.is_valid(), form.errors)
        staff_member = form.save(commit=False)
        staff_member.user = user
        staff_member.save()
        self.assertTrue(staff_member.is_holiday_off)

        edit_form = StaffMemberForm(instance=staff_member)
        self.assertTrue(edit_form["is_holiday_off"].value())
        self.assertEqual(form.cleaned_data["regular_days_off"], [0, 2])


class StaffMemberListTests(TestCase):
    def test_staff_members_are_grouped_by_ability_level(self):
        user = get_user_model().objects.create_user(username="ability-list-user", password="x")
        StaffMember.objects.create(user=user, name="管理者スタッフ", ability_level=5)
        StaffMember.objects.create(user=user, name="自立スタッフ", ability_level=2)
        self.client.force_login(user)

        response = self.client.get(reverse("staff:list"))

        content = response.content.decode()
        self.assertContains(response, "Lv.5 管理者スタッフ")
        self.assertContains(response, "Lv.2 自立スタッフ")
        self.assertContains(response, "<details", count=2)
        self.assertLess(content.index("Lv.5 管理者スタッフ"), content.index("Lv.2 自立スタッフ"))

    def test_holiday_off_is_displayed_after_regular_days(self):
        user = get_user_model().objects.create_user(username="holiday-list-user", password="x")
        staff_member = StaffMember.objects.create(
            user=user, name="祝日表示", is_holiday_off=True
        )
        staff_member.regular_days_off.create(day_of_week=0)
        self.client.force_login(user)

        response = self.client.get(reverse("staff:list"))

        content = response.content.decode()
        self.assertContains(response, "<span class=\"inline-flex h-5 min-w-5 items-center justify-center rounded px-1.5 text-[11px] font-bold leading-none bg-slate-100 text-slate-600\">月</span>", html=True)
        self.assertContains(response, "<span class=\"inline-flex h-5 min-w-5 items-center justify-center rounded bg-red-100 px-1.5 text-[11px] font-bold leading-none text-red-600\">祝</span>", html=True)
        self.assertLess(content.index(">月</span>"), content.index(">祝</span>"))
