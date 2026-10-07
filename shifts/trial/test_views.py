from datetime import date
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from shifts.models import ShiftPlan, ShiftResult
from shifts.shift_generation.types import GeneratedShift, ShiftGenerationResult

from .state import TRIAL_SESSION_KEY


class ShiftPlanTrialViewTests(TestCase):
    def get_response(self, reference_date=date(2026, 10, 6)):
        with patch(
            "shifts.trial.views.timezone.localdate",
            return_value=reference_date,
        ):
            return self.client.get(reverse("shifts:trial"))

    def post(self, data, reference_date=date(2026, 10, 6)):
        with patch(
            "shifts.trial.views.timezone.localdate",
            return_value=reference_date,
        ):
            return self.client.post(reverse("shifts:trial"), data)

    @staticmethod
    def get_cell(response, staff_id, target_date):
        row = next(
            row
            for row in response.context["staff_rows"]
            if row["staff_member"].id == staff_id
        )
        return next(cell for cell in row["cells"] if cell["date"] == target_date)

    def test_anonymous_user_can_open_trial_with_normal_editor_ui(self):
        response = self.get_response()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["month_dates"][0], date(2026, 11, 1))
        self.assertContains(response, "シフト作成の流れ")
        self.assertContains(response, "保存")
        self.assertContains(response, "シフト生成")
        self.assertContains(response, "リセット")
        self.assertContains(response, "看護師01")
        self.assertContains(response, "CSVダウンロード")
        self.assertContains(response, "シフト条件を編集")
        self.assertContains(response, "trial-disabled-readable")
        self.assertContains(response, 'title="体験版では利用できません"')
        self.assertNotContains(
            response,
            "※体験版の入力内容は、このブラウザ内で一時的に保存されます。",
        )

        content = response.content.decode()
        self.assertIn("data-generation-target-checkbox", content)
        self.assertIn("checked", content)
        self.assertIn("disabled", content)

    def test_top_page_links_to_trial(self):
        response = self.client.get(reverse("core:top_page"))

        self.assertContains(response, reverse("shifts:trial"))
        self.assertContains(response, "登録なしでお試し")

    def test_save_uses_session_without_creating_shift_models(self):
        target_date = date(2026, 11, 3)
        field_name = f"shift_1_{target_date.isoformat()}"

        response = self.post({"action": "save", field_name: "off_request"})

        self.assertRedirects(response, reverse("shifts:trial"))
        self.assertEqual(ShiftPlan.objects.count(), 0)
        self.assertEqual(ShiftResult.objects.count(), 0)
        self.assertEqual(
            self.client.session[TRIAL_SESSION_KEY]["saved_assignments"],
            {f"1|{target_date.isoformat()}": "off_request"},
        )

        response = self.get_response()
        self.assertEqual(
            self.get_cell(response, 1, target_date)["value"],
            ShiftResult.ShiftTypeChoices.OFF_REQUEST,
        )

    def test_generate_uses_saved_input_and_keeps_result_in_session(self):
        saved_date = date(2026, 11, 3)
        generated_date = date(2026, 11, 4)
        generation_result = ShiftGenerationResult(
            status="success",
            shifts=[
                GeneratedShift(1, saved_date, ShiftResult.ShiftTypeChoices.OFF_REQUEST),
                GeneratedShift(1, generated_date, ShiftResult.ShiftTypeChoices.DAY),
            ],
        )

        with patch(
            "shifts.trial.views.generate_with_optimizer_api",
            return_value=generation_result,
        ) as generate:
            response = self.post(
                {
                    "action": "generate",
                    f"shift_1_{saved_date.isoformat()}": "off_request",
                }
            )

        self.assertRedirects(response, reverse("shifts:trial"))
        context = generate.call_args.args[0]
        self.assertEqual(
            context.fixed_assignments[(1, saved_date)],
            ShiftResult.ShiftTypeChoices.OFF_REQUEST,
        )
        self.assertEqual(
            self.client.session[TRIAL_SESSION_KEY]["generated_assignments"][
                f"1|{generated_date.isoformat()}"
            ],
            ShiftResult.ShiftTypeChoices.DAY,
        )
        self.assertEqual(ShiftPlan.objects.count(), 0)
        self.assertEqual(ShiftResult.objects.count(), 0)

        response = self.get_response()
        self.assertEqual(
            self.get_cell(response, 1, generated_date)["value"],
            ShiftResult.ShiftTypeChoices.DAY,
        )

    def test_reset_to_manual_discards_generated_assignments(self):
        target_date = date(2026, 11, 3)
        session = self.client.session
        session[TRIAL_SESSION_KEY] = {
            "year": 2026,
            "month": 11,
            "saved_assignments": {f"1|{target_date.isoformat()}": "off_request"},
            "generated_assignments": {"1|2026-11-04": "day"},
            "generation_issues": [],
        }
        session.save()

        response = self.post({"action": "reset_to_manual"})

        self.assertRedirects(response, reverse("shifts:trial"))
        state = self.client.session[TRIAL_SESSION_KEY]
        self.assertEqual(state["generated_assignments"], {})
        self.assertEqual(
            state["saved_assignments"],
            {f"1|{target_date.isoformat()}": "off_request"},
        )

    def test_reset_to_base_clears_trial_session(self):
        session = self.client.session
        session[TRIAL_SESSION_KEY] = {
            "year": 2026,
            "month": 11,
            "saved_assignments": {"1|2026-11-03": "day"},
            "generated_assignments": {},
            "generation_issues": [],
        }
        session.save()

        response = self.post({"action": "reset_to_base"})

        self.assertRedirects(response, reverse("shifts:trial"))
        self.assertNotIn(TRIAL_SESSION_KEY, self.client.session)
