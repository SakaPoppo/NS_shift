from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from .constants import MAX_ACTIVE_STAFF_COUNT
from .forms import BulkStaffSetupForm, StaffMemberForm
from .models import StaffMember, StaffRegularDayOff


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
    def test_bulk_create_link_is_displayed_when_staff_can_be_created(self):
        user = get_user_model().objects.create_user(username="bulk-link-user", password="x")
        self.client.force_login(user)

        response = self.client.get(reverse("staff:list"))

        self.assertContains(response, reverse("staff:bulk_create"))
        self.assertContains(response, reverse("staff:bulk_edit"))
        self.assertContains(response, "一括登録")
        self.assertContains(response, "一括編集")

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

    def test_bulk_create_link_is_disabled_when_active_staff_limit_is_reached(self):
        self.create_staff_members(MAX_ACTIVE_STAFF_COUNT)

        response = self.client.get(reverse("staff:list"))

        self.assertNotContains(response, f'href="{reverse("staff:bulk_create")}"')
        self.assertContains(response, "一括登録")


class BulkStaffSetupFormTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bulk-setup-user",
            password="password123",
        )

    def setup_data(self, **overrides):
        data = {}
        for level in range(5, 0, -1):
            data.update(
                {
                    f"level_{level}_count": 0,
                    f"level_{level}_leader_count": 0,
                }
            )
        data.update(overrides)
        return data

    def test_generates_members_in_descending_level_order_without_saving(self):
        form = BulkStaffSetupForm(
            data=self.setup_data(
                level_5_count=1,
                level_4_count=5,
                level_2_count=1,
            ),
            user=self.user,
        )

        self.assertTrue(form.is_valid(), form.errors)
        initial_data = form.build_member_initial_data()

        self.assertEqual(StaffMember.objects.count(), 0)
        self.assertEqual([item["name"] for item in initial_data], ["No name"] * 7)
        self.assertEqual([item["ability_level"] for item in initial_data], [5, 4, 4, 4, 4, 4, 2])
        self.assertTrue(all(item["role"] == StaffMember.RoleChoices.MEMBER for item in initial_data))
        self.assertTrue(all(item["can_night_shift"] is True for item in initial_data))
        self.assertTrue(all(item["gender"] == "female" for item in initial_data))
        self.assertTrue(all(item["job"] == "nurse" for item in initial_data))
        self.assertTrue(all(item["regular_days_off"] == [] for item in initial_data))
        self.assertTrue(all(item["is_holiday_off"] is False for item in initial_data))

    def test_has_leader_setup_fields_and_no_night_off_field_for_each_ability_level(self):
        form = BulkStaffSetupForm(data=self.setup_data(level_3_count=1), user=self.user)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("level_3_all_leaders", form.fields)
        self.assertIn("level_3_use_leader_count", form.fields)
        self.assertIn("level_3_leader_count", form.fields)
        self.assertNotIn("level_3_night_off_count", form.fields)

    def test_generates_all_members_as_leaders_when_all_leaders_is_selected(self):
        form = BulkStaffSetupForm(
            data=self.setup_data(level_4_count=3, level_4_all_leaders=True),
            user=self.user,
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            [item["role"] for item in form.build_member_initial_data()],
            [StaffMember.RoleChoices.LEADER] * 3,
        )

    def test_generates_requested_number_of_leaders_from_top_of_level(self):
        form = BulkStaffSetupForm(
            data=self.setup_data(
                level_4_count=5,
                level_4_use_leader_count=True,
                level_4_leader_count=2,
            ),
            user=self.user,
        )

        self.assertTrue(form.is_valid(), form.errors)
        initial_data = form.build_member_initial_data()
        self.assertEqual(
            [item["role"] for item in initial_data],
            [
                StaffMember.RoleChoices.LEADER,
                StaffMember.RoleChoices.LEADER,
                StaffMember.RoleChoices.MEMBER,
                StaffMember.RoleChoices.MEMBER,
                StaffMember.RoleChoices.MEMBER,
            ],
        )
        self.assertEqual([item["ability_level"] for item in initial_data], [4] * 5)

    def test_rejects_leader_count_exceeding_level_staff_count(self):
        form = BulkStaffSetupForm(
            data=self.setup_data(
                level_3_count=1,
                level_3_use_leader_count=True,
                level_3_leader_count=2,
            ),
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("level_3_leader_count", form.errors)

    def test_rejects_all_leaders_and_specified_leader_count_together(self):
        form = BulkStaffSetupForm(
            data=self.setup_data(
                level_3_count=2,
                level_3_all_leaders=True,
                level_3_use_leader_count=True,
                level_3_leader_count=1,
            ),
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("level_3_all_leaders", form.errors)
        self.assertIn("level_3_use_leader_count", form.errors)

    def test_rejects_zero_total_count(self):
        form = BulkStaffSetupForm(data=self.setup_data(), user=self.user)

        self.assertFalse(form.is_valid())
        self.assertIn("スタッフ数の合計は1人以上", form.non_field_errors()[0])

    def test_rejects_count_exceeding_active_staff_limit(self):
        StaffMember.objects.bulk_create(
            [StaffMember(user=self.user, name=f"既存{index}") for index in range(39)]
        )
        form = BulkStaffSetupForm(
            data=self.setup_data(level_2_count=2),
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("40人以下", form.non_field_errors()[0])


class BulkStaffCreateViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bulk-create-user",
            password="password123",
        )
        self.other_user = get_user_model().objects.create_user(
            username="other-user",
            password="password123",
        )
        self.client.force_login(self.user)

    def setup_data(self, **overrides):
        data = {}
        for level in range(5, 0, -1):
            data.update(
                {
                    f"level_{level}_count": "0",
                    f"level_{level}_leader_count": "0",
                }
            )
        data.update(overrides)
        return data

    def start_bulk_create(self, **overrides):
        response = self.client.post(
            reverse("staff:bulk_create"),
            self.setup_data(**overrides),
        )
        self.assertRedirects(response, reverse("staff:bulk_create_confirm"))
        return self.client.session["bulk_staff_member_initial_data"]

    def formset_post_data(self, initial_data, changes=None, total_forms=None):
        changes = changes or {}
        total_forms = len(initial_data) if total_forms is None else total_forms
        data = {
            "form-TOTAL_FORMS": str(total_forms),
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": str(MAX_ACTIVE_STAFF_COUNT),
        }
        for index, initial in enumerate(initial_data):
            values = {**initial, **changes.get(index, {})}
            prefix = f"form-{index}"
            data.update(
                {
                    f"{prefix}-name": values["name"],
                    f"{prefix}-gender": values["gender"],
                    f"{prefix}-job": values["job"],
                    f"{prefix}-role": values["role"],
                    f"{prefix}-ability_level": str(values["ability_level"]),
                    f"{prefix}-can_night_shift": str(values["can_night_shift"]),
                }
            )
            if values.get("is_holiday_off"):
                data[f"{prefix}-is_holiday_off"] = "on"
            if values.get("regular_days_off"):
                data[f"{prefix}-regular_days_off"] = values["regular_days_off"]
        return data

    def test_setup_generates_formset_without_creating_staff(self):
        initial_data = self.start_bulk_create(level_5_count="1", level_3_count="2")

        self.assertEqual(StaffMember.objects.filter(user=self.user).count(), 0)
        self.assertEqual(len(initial_data), 3)
        response = self.client.get(reverse("staff:bulk_create_confirm"))
        self.assertEqual(len(response.context["formset"].forms), 3)

    def test_bulk_setup_page_displays_level_table_and_remaining_staff_count(self):
        response = self.client.get(reverse("staff:bulk_create"))

        self.assertContains(response, "Lv5")
        self.assertContains(response, "管理者")
        self.assertContains(response, "Lv1")
        self.assertContains(response, "新人")
        self.assertContains(response, 'data-staff-count-input="true"', count=5)
        self.assertContains(response, "全員リーダー")
        self.assertContains(response, "指定した数だけリーダーにする")
        self.assertNotContains(response, "夜勤不可人数")
        self.assertNotContains(response, "登録可能：あと")
        self.assertContains(response, "スタッフ確認画面へ進む")

    def test_bulk_confirm_page_displays_compact_table_and_registration_count(self):
        self.start_bulk_create(level_5_count="1", level_3_count="1")

        response = self.client.get(reverse("staff:bulk_create_confirm"))

        self.assertContains(response, "登録内容を確認")
        self.assertContains(response, "登録前のスタッフ情報一覧")
        self.assertContains(response, "固定休")
        self.assertContains(response, "役割")
        self.assertNotContains(response, "リーダー一括設定")
        self.assertNotContains(response, "名前未設定")
        self.assertContains(response, "2人を一括登録")
        self.assertContains(response, 'name="form-TOTAL_FORMS"')

    def test_post_uses_session_initial_data_and_rejects_an_empty_staff_row(self):
        initial_data = self.start_bulk_create(level_2_count="2")
        empty_row = {
            "name": "",
            "gender": "",
            "job": "",
            "ability_level": "",
            "can_night_shift": "",
        }

        response = self.client.post(
            reverse("staff:bulk_create_confirm"),
            self.formset_post_data(initial_data, changes={0: empty_row}),
        )

        self.assertEqual(response.status_code, 200)
        formset = response.context["formset"]
        self.assertEqual(formset.forms[0].initial["name"], "No name")
        self.assertIn("name", formset.errors[0])
        self.assertFalse(StaffMember.objects.filter(user=self.user).exists())

    def test_rejects_total_forms_tampering_that_adds_a_staff_member(self):
        initial_data = self.start_bulk_create(level_2_count="2")

        response = self.client.post(
            reverse("staff:bulk_create_confirm"),
            self.formset_post_data(initial_data, total_forms=3),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "登録対象のスタッフ数が不正です。")
        self.assertFalse(StaffMember.objects.filter(user=self.user).exists())

    def test_rejects_total_forms_tampering_that_removes_a_staff_member(self):
        initial_data = self.start_bulk_create(level_2_count="2")

        response = self.client.post(
            reverse("staff:bulk_create_confirm"),
            self.formset_post_data(initial_data, total_forms=1),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "登録対象のスタッフ数が不正です。")
        self.assertFalse(StaffMember.objects.filter(user=self.user).exists())

    def test_bulk_create_saves_members_with_expected_levels_and_individual_settings(self):
        initial_data = self.start_bulk_create(
            level_4_count="5",
            level_4_use_leader_count="on",
            level_4_leader_count="2",
            level_1_count="1",
        )

        response = self.client.post(
            reverse("staff:bulk_create_confirm"),
            self.formset_post_data(
                initial_data,
                changes={
                    0: {"role": "member", "regular_days_off": [0, 2], "is_holiday_off": True},
                    1: {"can_night_shift": False},
                },
            ),
        )

        self.assertRedirects(response, reverse("staff:list"))
        staff_members = list(StaffMember.objects.filter(user=self.user).order_by("id"))
        self.assertEqual(len(staff_members), 6)
        self.assertEqual([member.name for member in staff_members], ["No name"] * 6)
        self.assertEqual([member.ability_level for member in staff_members], [4, 4, 4, 4, 4, 1])
        self.assertEqual([member.role for member in staff_members], ["member", "leader", "member", "member", "member", "member"])
        self.assertEqual([member.can_night_shift for member in staff_members], [True, False, True, True, True, True])
        self.assertTrue(staff_members[0].is_holiday_off)
        self.assertEqual(
            list(
                StaffRegularDayOff.objects.filter(staff_member=staff_members[0])
                .order_by("day_of_week")
                .values_list("day_of_week", flat=True)
            ),
            [0, 2],
        )
        self.assertTrue(all(member.user == self.user for member in staff_members))
        self.assertFalse(StaffMember.objects.filter(user=self.other_user).exists())

    def test_save_rechecks_active_staff_limit_and_creates_nothing_when_it_is_exceeded(self):
        initial_data = self.start_bulk_create(level_2_count="2")
        StaffMember.objects.bulk_create(
            [StaffMember(user=self.user, name=f"既存{index}") for index in range(39)]
        )

        response = self.client.post(
            reverse("staff:bulk_create_confirm"),
            self.formset_post_data(initial_data),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "登録できる在籍スタッフは40人までです。")
        self.assertEqual(StaffMember.objects.filter(user=self.user).count(), 39)

    def test_failure_during_bulk_save_rolls_back_every_staff_member(self):
        initial_data = self.start_bulk_create(level_2_count="2")

        with patch(
            "staff.views.sync_regular_days_off",
            side_effect=[None, RuntimeError("固定休の保存に失敗")],
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("staff:bulk_create_confirm"),
                    self.formset_post_data(initial_data),
                )

        self.assertFalse(StaffMember.objects.filter(user=self.user).exists())

    def test_existing_normal_create_and_edit_flow_still_works(self):
        create_data = {
            "name": "通常登録スタッフ",
            "gender": "female",
            "job": "nurse",
            "role": "member",
            "ability_level": "2",
            "can_night_shift": "True",
        }
        response = self.client.post(reverse("staff:create"), create_data)
        self.assertRedirects(response, reverse("staff:list"))

        staff_member = StaffMember.objects.get(user=self.user, name="通常登録スタッフ")
        create_data["name"] = "通常編集スタッフ"
        response = self.client.post(
            reverse("staff:edit", args=[staff_member.pk]),
            create_data,
        )

        self.assertRedirects(response, reverse("staff:list"))
        staff_member.refresh_from_db()
        self.assertEqual(staff_member.name, "通常編集スタッフ")


class BulkStaffEditViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bulk-edit-user",
            password="password123",
        )
        self.other_user = get_user_model().objects.create_user(
            username="bulk-edit-other-user",
            password="password123",
        )
        self.client.force_login(self.user)

    def create_staff(self, name, *, user=None, ability_level=2, is_active=True):
        return StaffMember.objects.create(
            user=user or self.user,
            name=name,
            gender=StaffMember.GenderChoices.FEMALE,
            job=StaffMember.JobChoices.NURSE,
            role=StaffMember.RoleChoices.MEMBER,
            ability_level=ability_level,
            can_night_shift=True,
            is_active=is_active,
        )

    def formset_post_data(self, staff_members, changes=None):
        changes = changes or {}
        data = {
            "form-TOTAL_FORMS": str(len(staff_members)),
            "form-INITIAL_FORMS": str(len(staff_members)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, staff_member in enumerate(staff_members):
            values = {
                "id": staff_member.pk,
                "name": staff_member.name,
                "gender": staff_member.gender,
                "job": staff_member.job,
                "role": staff_member.role,
                "ability_level": staff_member.ability_level,
                "can_night_shift": staff_member.can_night_shift,
                "regular_days_off": list(
                    staff_member.regular_days_off.values_list("day_of_week", flat=True)
                ),
                "is_holiday_off": staff_member.is_holiday_off,
                "delete_staff": False,
            }
            values.update(changes.get(index, {}))
            prefix = f"form-{index}"
            data.update(
                {
                    f"{prefix}-id": values["id"],
                    f"{prefix}-name": values["name"],
                    f"{prefix}-gender": values["gender"],
                    f"{prefix}-job": values["job"],
                    f"{prefix}-role": values["role"],
                    f"{prefix}-ability_level": values["ability_level"],
                    f"{prefix}-can_night_shift": str(values["can_night_shift"]),
                }
            )
            if values["regular_days_off"]:
                data[f"{prefix}-regular_days_off"] = values["regular_days_off"]
            if values["is_holiday_off"]:
                data[f"{prefix}-is_holiday_off"] = "on"
            if values["delete_staff"]:
                data[f"{prefix}-delete_staff"] = "on"
        return data

    def active_staff_members(self):
        return list(
            StaffMember.objects.filter(user=self.user, is_active=True).order_by(
                "-ability_level", "id"
            )
        )

    def test_displays_only_logged_in_users_active_staff_members(self):
        visible_staff = self.create_staff("表示スタッフ")
        self.create_staff("非表示スタッフ", is_active=False)
        self.create_staff("他ユーザースタッフ", user=self.other_user)

        response = self.client.get(reverse("staff:bulk_edit"))

        self.assertContains(response, visible_staff.name)
        self.assertNotContains(response, "非表示スタッフ")
        self.assertNotContains(response, "他ユーザースタッフ")
        self.assertContains(response, "削除予定：")

    def test_updates_staff_fields_and_regular_days_off(self):
        staff_member = self.create_staff("更新前")
        staff_members = self.active_staff_members()

        response = self.client.post(
            reverse("staff:bulk_edit"),
            self.formset_post_data(
                staff_members,
                changes={
                    0: {
                        "name": "更新後",
                        "role": StaffMember.RoleChoices.LEADER,
                        "ability_level": 5,
                        "can_night_shift": False,
                        "regular_days_off": [0, 2],
                    }
                },
            ),
        )

        self.assertRedirects(response, reverse("staff:list"))
        staff_member.refresh_from_db()
        self.assertEqual(staff_member.name, "更新後")
        self.assertEqual(staff_member.role, StaffMember.RoleChoices.LEADER)
        self.assertEqual(staff_member.ability_level, 5)
        self.assertFalse(staff_member.can_night_shift)
        self.assertEqual(
            list(staff_member.regular_days_off.values_list("day_of_week", flat=True)),
            [0, 2],
        )

    def test_logically_deletes_marked_staff_without_touching_regular_days_off(self):
        deleted_staff = self.create_staff("削除対象")
        deleted_staff.regular_days_off.create(day_of_week=1)
        updated_staff = self.create_staff("更新対象")
        staff_members = self.active_staff_members()
        deleted_index = staff_members.index(deleted_staff)
        updated_index = staff_members.index(updated_staff)

        response = self.client.post(
            reverse("staff:bulk_edit"),
            self.formset_post_data(
                staff_members,
                changes={
                    deleted_index: {"delete_staff": True, "name": "保存しない変更"},
                    updated_index: {"name": "通常更新後"},
                },
            ),
        )

        self.assertRedirects(response, reverse("staff:list"))
        deleted_staff.refresh_from_db()
        updated_staff.refresh_from_db()
        self.assertFalse(deleted_staff.is_active)
        self.assertEqual(deleted_staff.name, "削除対象")
        self.assertTrue(StaffMember.objects.filter(pk=deleted_staff.pk).exists())
        self.assertEqual(
            list(deleted_staff.regular_days_off.values_list("day_of_week", flat=True)),
            [1],
        )
        self.assertEqual(updated_staff.name, "通常更新後")

    def test_rejects_other_users_staff_member_id(self):
        own_staff = self.create_staff("自分のスタッフ")
        other_staff = self.create_staff("他人のスタッフ", user=self.other_user)
        data = self.formset_post_data(self.active_staff_members())
        data["form-0-id"] = other_staff.pk
        data["form-0-name"] = "不正更新"

        response = self.client.post(reverse("staff:bulk_edit"), data)

        self.assertEqual(response.status_code, 200)
        own_staff.refresh_from_db()
        other_staff.refresh_from_db()
        self.assertEqual(own_staff.name, "自分のスタッフ")
        self.assertEqual(other_staff.name, "他人のスタッフ")

    def test_rolls_back_updates_and_logical_deletions_when_saving_fails(self):
        deleted_staff = self.create_staff("削除対象", ability_level=5)
        updated_staff = self.create_staff("更新対象", ability_level=2)
        staff_members = self.active_staff_members()

        with patch("staff.views.sync_regular_days_off", side_effect=RuntimeError("保存失敗")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("staff:bulk_edit"),
                    self.formset_post_data(
                        staff_members,
                        changes={0: {"delete_staff": True}, 1: {"name": "更新後"}},
                    ),
                )

        deleted_staff.refresh_from_db()
        updated_staff.refresh_from_db()
        self.assertTrue(deleted_staff.is_active)
        self.assertEqual(updated_staff.name, "更新対象")
