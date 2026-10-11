from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import DatabaseError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse

from shifts.models import ShiftPlan, ShiftRule
from shifts.shift_generation.types import ShiftGenerationError, ShiftGenerationResult
from shifts.trial.rate_limit import TRIAL_VISITOR_SESSION_KEY, TrialRateLimitResult
from staff.models import StaffMember

from .models import UserEvent
from .services import track_event


class UserEventTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="events", password="password123"
        )
        self.request = RequestFactory().get("/")
        self.request.user = AnonymousUser()
        self.request.session = {}

    def record(self, event=UserEvent.EventType.STAFF_CREATED, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            track_event(event, **kwargs)

    def post(self, url, data):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data)

    def events(self):
        return list(
            UserEvent.objects.order_by("pk").values_list("event_type", flat=True)
        )

    def test_repeated_events_append(self):
        for _ in range(2):
            self.record(user=self.user)
        self.assertEqual(UserEvent.objects.filter(user=self.user).count(), 2)

    def test_anonymous_visitor(self):
        visitor = uuid4()
        self.request.session[TRIAL_VISITOR_SESSION_KEY] = str(visitor)
        self.record(request=self.request)
        event = UserEvent.objects.get()
        self.assertEqual(event.visitor_id, visitor)
        self.assertIsNone(event.user_id)

    def test_authenticated_user_without_creating_visitor(self):
        self.request.user = self.user
        self.record(request=self.request)
        event = UserEvent.objects.get()
        self.assertEqual(event.user_id, self.user.pk)
        self.assertIsNone(event.visitor_id)
        self.assertEqual(self.request.session, {})

    def test_both_identifiers(self):
        self.request.user = self.user
        self.request.session[TRIAL_VISITOR_SESSION_KEY] = str(uuid4())
        self.record(request=self.request)
        event = UserEvent.objects.get()
        self.assertEqual(event.user_id, self.user.pk)
        self.assertEqual(
            str(event.visitor_id), self.request.session[TRIAL_VISITOR_SESSION_KEY]
        )

    def test_rollback_discards_callback(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    track_event(UserEvent.EventType.STAFF_CREATED, user=self.user)
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(UserEvent.objects.exists())

    def test_waits_for_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                track_event(UserEvent.EventType.STAFF_CREATED, user=self.user)
                self.assertFalse(UserEvent.objects.exists())
        self.assertEqual(UserEvent.objects.count(), 1)

    def test_insert_failure_is_logged_and_connection_remains_usable(self):
        with patch(
            "analytics.services.UserEvent.objects.create",
            side_effect=DatabaseError("failed"),
        ):
            with self.assertLogs("analytics.services", level="ERROR"):
                self.record(user=self.user)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_invalid_visitor_does_not_raise(self):
        self.request.session[TRIAL_VISITOR_SESSION_KEY] = "invalid"
        with self.assertLogs("analytics.services", level="ERROR"):
            self.record(request=self.request)

    def test_user_deletion_sets_null_and_keeps_history(self):
        self.record(user=self.user)
        self.user.delete()
        self.assertIsNone(UserEvent.objects.get().user_id)

    def test_trial_get_and_signup_keep_visitor(self):
        with self.captureOnCommitCallbacks(execute=True):
            for _ in range(2):
                self.assertEqual(
                    self.client.get(reverse("shifts:trial")).status_code, 200
                )
        visitor = UUID(self.client.session[TRIAL_VISITOR_SESSION_KEY])
        response = self.post(
            reverse("accounts:signup"),
            {
                "username": "signed-up",
                "email": "signup@example.com",
                "password1": "SafePassword2468!",
                "password2": "SafePassword2468!",
            },
        )
        self.assertEqual(response.status_code, 302)
        event = UserEvent.objects.get(event_type="signup_completed")
        self.assertEqual(event.visitor_id, visitor)
        self.assertEqual(event.user.username, "signed-up")
        self.assertEqual(UserEvent.objects.filter(event_type="trial_opened").count(), 2)

    def test_invalid_signup_and_staff_do_not_record_success(self):
        self.post(reverse("accounts:signup"), {})
        self.client.force_login(self.user)
        self.post(reverse("staff:create"), {})
        self.post(reverse("shifts:create"), {})
        self.assertEqual(self.events(), [])

    def setup_plan(self):
        self.client.force_login(self.user)
        plan = ShiftPlan.objects.create(user=self.user, year=2026, month=8)
        StaffMember.objects.create(user=self.user, name="Test", gender="male")
        ShiftRule.objects.create(
            shift_plan=plan,
            required_day_staff=1,
            required_night_staff=0,
            off_days_per_staff=0,
            max_consecutive_work_days=5,
        )
        return plan

    def test_generation_and_regeneration(self):
        plan = self.setup_plan()
        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=ShiftGenerationResult(status="success", shifts=[]),
        ):
            for _ in range(2):
                self.assertEqual(
                    self.post(
                        reverse("shifts:edit", args=[plan.pk]), {"action": "generate"}
                    ).status_code,
                    302,
                )
        self.assertEqual(
            self.events(), ["generation_started", "generation_succeeded"] * 2
        )

    def test_generation_failure(self):
        plan = self.setup_plan()
        with patch(
            "shifts.views.generate_and_save_shift",
            side_effect=ShiftGenerationError("failed"),
        ):
            self.assertEqual(
                self.post(
                    reverse("shifts:edit", args=[plan.pk]), {"action": "generate"}
                ).status_code,
                200,
            )
        self.assertEqual(self.events(), ["generation_started", "generation_failed"])

    def test_save_failure_rolls_back_success_and_propagates(self):
        plan = self.setup_plan()
        with patch(
            "shifts.views.generate_and_save_shift",
            return_value=ShiftGenerationResult(status="success", shifts=[]),
        ):
            with patch(
                "shifts.views.sync_next_month_boundary_assignments",
                side_effect=RuntimeError("save failed"),
            ):
                with self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaises(RuntimeError):
                        self.client.post(
                            reverse("shifts:edit", args=[plan.pk]),
                            {"action": "generate"},
                        )
        self.assertEqual(self.events(), ["generation_started", "generation_failed"])

    def test_generation_validation_does_not_start(self):
        plan = self.setup_plan()
        with patch(
            "shifts.views.validate_manual_assignments", return_value=["invalid"]
        ):
            self.post(reverse("shifts:edit", args=[plan.pk]), {"action": "generate"})
        self.assertEqual(self.events(), [])

    def test_trial_results(self):
        with patch(
            "shifts.trial.views.generate_with_optimizer_api",
            return_value=ShiftGenerationResult(status="success", shifts=[]),
        ):
            self.assertEqual(
                self.post(reverse("shifts:trial"), {"action": "generate"}).status_code,
                302,
            )
        with patch(
            "shifts.trial.views.generate_with_optimizer_api",
            side_effect=ShiftGenerationError("failed"),
        ):
            self.assertEqual(
                self.post(reverse("shifts:trial"), {"action": "generate"}).status_code,
                200,
            )
        self.assertEqual(
            self.events(),
            [
                "trial_generation_started",
                "trial_generation_succeeded",
                "trial_generation_started",
                "trial_generation_failed",
            ],
        )

    def test_trial_rate_limit_does_not_start(self):
        with patch(
            "shifts.trial.views.consume_trial_generation_quota",
            return_value=TrialRateLimitResult(allowed=False, message="limited"),
        ):
            self.assertEqual(
                self.post(reverse("shifts:trial"), {"action": "generate"}).status_code,
                429,
            )
        self.assertEqual(self.events(), [])

    def test_trial_validation_does_not_start(self):
        with patch(
            "shifts.trial.views.validate_manual_assignments", return_value=["invalid"]
        ):
            self.post(reverse("shifts:trial"), {"action": "generate"})
        self.assertEqual(self.events(), [])

    def test_logging_failure_does_not_break_generation(self):
        plan = self.setup_plan()
        with patch(
            "analytics.services.UserEvent.objects.create",
            side_effect=DatabaseError("failed"),
        ):
            with patch(
                "shifts.views.generate_and_save_shift",
                return_value=ShiftGenerationResult(status="success", shifts=[]),
            ):
                with self.assertLogs("analytics.services", level="ERROR"):
                    response = self.post(
                        reverse("shifts:edit", args=[plan.pk]), {"action": "generate"}
                    )
        self.assertEqual(response.status_code, 302)

    def test_csv_response_is_recorded(self):
        plan = self.setup_plan()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.get(reverse("shifts:export_csv", args=[plan.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events(), ["shift_exported"])

    def test_staff_and_plan_creation(self):
        self.client.force_login(self.user)
        response = self.post(
            reverse("staff:create"),
            {
                "name": "Test",
                "gender": "female",
                "job": "nurse",
                "role": "member",
                "ability_level": "2",
                "can_night_shift": "True",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.post(reverse("shifts:create"), {"year": "2026", "month": "10"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.events(), ["staff_created", "shift_plan_created"])

    def test_conditions_success_and_invalid_input(self):
        plan = self.setup_plan()
        data = {
            "required_day_staff": "1",
            "required_night_staff": "0",
            "off_days_per_staff": "0",
            "max_consecutive_work_days": "5",
            "night_shift_next_day_off": "True",
            "date_rule_total_forms": "0",
        }
        for day in range(8):
            data[f"weekday-{day}-selected"] = "0"
            data[f"weekday-{day}-day_of_week"] = str(day)
        response = self.post(reverse("shifts:conditions", args=[plan.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.post(
            reverse("shifts:conditions", args=[plan.pk]),
            {**data, "required_day_staff": "invalid"},
        )
        self.assertEqual(self.events(), ["shift_rules_saved"])

    def test_bulk_staff_creation_records_each_saved_staff(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["bulk_staff_member_initial_data"] = [{"name": "One"}, {"name": "Two"}]
        session.save()
        data = {
            "form-TOTAL_FORMS": "2",
            "form-INITIAL_FORMS": "0",
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for i in range(2):
            for key, value in {
                "name": f"Test{i}",
                "gender": "female",
                "job": "nurse",
                "role": "member",
                "ability_level": "2",
                "can_night_shift": "True",
            }.items():
                data[f"form-{i}-{key}"] = value
        response = self.post(reverse("staff:bulk_create_confirm"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.events(), ["staff_created"] * 2)
