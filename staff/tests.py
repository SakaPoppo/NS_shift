from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .constants import MAX_ACTIVE_STAFF_COUNT
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


class StaffMemberActiveLimitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staff-limit-user",
            password="password123",
        )
        self.client.force_login(self.user)

    def create_staff_members(self, count, is_active=True):
        return StaffMember.objects.bulk_create(
            [
                StaffMember(
                    user=self.user,
                    name=f"スタッフ{index}",
                    is_active=is_active,
                )
                for index in range(count)
            ]
        )

    def create_post_data(self):
        return {
            "name": "新規スタッフ",
            "gender": StaffMember.GenderChoices.FEMALE,
            "job": StaffMember.JobChoices.NURSE,
            "role": StaffMember.RoleChoices.MEMBER,
            "ability_level": StaffMember.AbilityLevelChoices.LEVEL_2,
            "can_night_shift": "True",
        }

    def active_staff_count(self):
        return StaffMember.objects.filter(user=self.user, is_active=True).count()

    def test_can_create_staff_when_there_are_thirty_nine_active_staff_members(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT - 1)

        response = self.client.post(reverse("staff:create"), self.create_post_data())

        self.assertRedirects(response, reverse("staff:list"))
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT)

    def test_cannot_access_create_page_when_there_are_forty_active_staff_members(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT)

        response = self.client.get(reverse("staff:create"), follow=True)

        self.assertRedirects(response, reverse("staff:list"))
        self.assertContains(response, "登録できる在籍スタッフは40人までです。")
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT)

    def test_existing_staff_members_are_preserved_when_active_count_exceeds_limit(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT + 1)

        response = self.client.get(reverse("staff:list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT + 1)

    def test_existing_staff_members_can_be_edited_when_active_count_exceeds_limit(self):
        staff_members = self.create_staff_members(MAX_ACTIVE_STAFF_COUNT + 1)

        response = self.client.get(reverse("staff:edit", args=[staff_members[0].pk]))

        self.assertEqual(response.status_code, 200)

    def test_inactive_staff_members_do_not_count_toward_the_limit(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT - 1)
        self.create_staff_members(3, is_active=False)

        response = self.client.post(reverse("staff:create"), self.create_post_data())

        self.assertRedirects(response, reverse("staff:list"))
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT)
        self.assertEqual(
            StaffMember.objects.filter(user=self.user, is_active=False).count(),
            3,
        )

    def test_direct_post_cannot_create_forty_first_active_staff_member(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT)

        response = self.client.post(reverse("staff:create"), self.create_post_data(), follow=True)

        self.assertRedirects(response, reverse("staff:list"))
        self.assertContains(response, "登録できる在籍スタッフは40人までです。")
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT)

    def test_cannot_create_when_count_is_still_forty_four_after_removing_staff(self):
        staff_members = self.create_staff_members(MAX_ACTIVE_STAFF_COUNT + 5)

        response = self.client.post(reverse("staff:delete", args=[staff_members[0].pk]))

        self.assertRedirects(response, reverse("staff:list"))
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT + 4)

        response = self.client.post(reverse("staff:create"), self.create_post_data())

        self.assertRedirects(response, reverse("staff:list"))
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT + 4)

    def test_can_create_after_removing_staff_from_forty_to_thirty_nine(self):
        staff_members = self.create_staff_members(MAX_ACTIVE_STAFF_COUNT)
        self.client.post(reverse("staff:delete", args=[staff_members[0].pk]))

        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT - 1)

        response = self.client.post(reverse("staff:create"), self.create_post_data())

        self.assertRedirects(response, reverse("staff:list"))
        self.assertEqual(self.active_staff_count(), MAX_ACTIVE_STAFF_COUNT)

    def test_list_displays_active_staff_count_and_limit(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT - 1)

        response = self.client.get(reverse("staff:list"))

        self.assertContains(
            response,
            f"登録スタッフ数：{MAX_ACTIVE_STAFF_COUNT - 1} / {MAX_ACTIVE_STAFF_COUNT}人",
        )

    def test_list_displays_actual_count_when_active_staff_count_exceeds_limit(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT + 5)

        response = self.client.get(reverse("staff:list"))

        self.assertContains(
            response,
            f"登録スタッフ数：{MAX_ACTIVE_STAFF_COUNT + 5} / {MAX_ACTIVE_STAFF_COUNT}人",
        )
        self.assertContains(response, "登録できる在籍スタッフは40人までです。")
