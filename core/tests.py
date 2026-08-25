from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from shifts.models import ShiftPlan
from staff.models import StaffMember


User = get_user_model()


class MainPageDashboardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="dashboard-user",
            password="password123",
        )
        self.client.force_login(self.user)
        self.url = reverse("core:main_page")

    def test_no_active_staff_guides_user_to_staff_creation(self):
        StaffMember.objects.create(
            user=self.user,
            name="退職済み",
            is_active=False,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.context["dashboard_state"], "setup_staff")
        self.assertEqual(response.context["staff_count"], 0)
        self.assertContains(response, "メインページ")
        self.assertContains(response, "管理する項目を選択してください")
        self.assertContains(response, "まずはこちら")
        self.assertContains(response, "clip-path:polygon")
        self.assertNotContains(response, 'class="badge')
        self.assertNotContains(response, "tooltip-open")
        self.assertNotContains(response, "tooltip-content")
        self.assertNotContains(response, reverse("staff:create"))
        self.assertContains(response, reverse("staff:list"))
        self.assertContains(response, "管理画面へ移動")
        self.assertContains(response, "スタッフが1人以上登録されると使用できます")
        self.assertContains(response, 'aria-disabled="true"')
        self.assertNotContains(response, "シフト作成はこちら")

    def test_staff_without_shift_plan_guides_user_to_shift_creation(self):
        StaffMember.objects.create(user=self.user, name="在籍スタッフ")

        response = self.client.get(self.url)

        self.assertEqual(response.context["dashboard_state"], "create_shift")
        self.assertEqual(response.context["staff_count"], 1)
        self.assertContains(response, "メインページ")
        self.assertContains(response, "管理する項目を選択してください")
        self.assertContains(response, "シフト作成はこちら")
        self.assertContains(response, "clip-path:polygon")
        self.assertContains(
            response,
            "group-has-[.staff-management-card:hover]/dashboard:ring-0",
        )
        self.assertContains(response, "staff-management-card")
        self.assertContains(response, reverse("shifts:list"))
        self.assertContains(response, reverse("staff:list"))

    def test_staff_and_shift_plan_show_normal_dashboard(self):
        StaffMember.objects.create(user=self.user, name="在籍スタッフ")
        ShiftPlan.objects.create(user=self.user, year=2026, month=8)

        response = self.client.get(self.url)

        self.assertEqual(response.context["dashboard_state"], "normal")
        self.assertEqual(response.context["staff_count"], 1)
        self.assertContains(response, "メインページ")
        self.assertContains(response, reverse("shifts:list"))
        self.assertContains(response, reverse("staff:list"))
        self.assertNotContains(response, "まずはこちら")
        self.assertNotContains(response, "シフト作成はこちら")
        self.assertContains(response, "hover:ring-2", count=2)

    def test_other_users_data_does_not_affect_dashboard_state(self):
        other_user = User.objects.create_user(
            username="other-dashboard-user",
            password="password123",
        )
        StaffMember.objects.create(user=other_user, name="他人のスタッフ")
        ShiftPlan.objects.create(user=other_user, year=2026, month=8)

        response = self.client.get(self.url)

        self.assertEqual(response.context["dashboard_state"], "setup_staff")
        self.assertEqual(response.context["staff_count"], 0)
