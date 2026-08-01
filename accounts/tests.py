from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import REGISTRATION_UNAVAILABLE_MESSAGE, SignUpForm


User = get_user_model()


class SignUpFormTests(TestCase):
    valid_data = {
        "username": "new-user",
        "email": "new@example.com",
        "password1": "SafePassword2468!",
        "password2": "SafePassword2468!",
    }

    def test_email_is_required(self):
        data = {**self.valid_data, "email": ""}

        form = SignUpForm(data=data)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["email"][0].code, "required")
        self.assertIn("メールアドレスを入力してください。", form.errors["email"])

    def test_duplicate_username_uses_generic_message(self):
        User.objects.create_user(
            username=self.valid_data["username"],
            email="existing@example.com",
            password="ExistingPassword2468!",
        )

        form = SignUpForm(data=self.valid_data)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["username"], [REGISTRATION_UNAVAILABLE_MESSAGE])

    def test_duplicate_email_uses_generic_message_case_insensitively(self):
        User.objects.create_user(
            username="existing-user",
            email="NEW@EXAMPLE.COM",
            password="ExistingPassword2468!",
        )

        form = SignUpForm(data=self.valid_data)

        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["email"], [REGISTRATION_UNAVAILABLE_MESSAGE])


class SignUpViewTests(TestCase):
    signup_url = reverse("accounts:signup")
    valid_data = {
        "username": "view-user",
        "email": "view@example.com",
        "password1": "SafePassword2468!",
        "password2": "SafePassword2468!",
    }

    def test_signup_page_marks_email_as_required(self):
        response = self.client.get(self.signup_url)

        self.assertContains(response, "メールアドレス")
        self.assertNotContains(response, "任意")

    def test_duplicate_errors_do_not_reveal_registration_status(self):
        User.objects.create_user(
            username=self.valid_data["username"],
            email="VIEW@EXAMPLE.COM",
            password="ExistingPassword2468!",
        )

        response = self.client.post(self.signup_url, self.valid_data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, REGISTRATION_UNAVAILABLE_MESSAGE, count=2)
        self.assertNotContains(response, "登録済み")
        self.assertNotContains(response, "すでに登録")

    def test_valid_signup_creates_and_logs_in_user(self):
        response = self.client.post(self.signup_url, self.valid_data)

        self.assertRedirects(response, reverse("core:main_page"))
        user = User.objects.get(username=self.valid_data["username"])
        self.assertEqual(user.email, self.valid_data["email"])
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
