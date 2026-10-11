from django.conf import settings
from django.db import models


class UserEvent(models.Model):
    class EventType(models.TextChoices):
        SIGNUP_COMPLETED = "signup_completed"
        STAFF_CREATED = "staff_created"
        SHIFT_PLAN_CREATED = "shift_plan_created"
        SHIFT_RULES_SAVED = "shift_rules_saved"
        GENERATION_STARTED = "generation_started"
        GENERATION_SUCCEEDED = "generation_succeeded"
        GENERATION_FAILED = "generation_failed"
        SHIFT_EXPORTED = "shift_exported"
        TRIAL_OPENED = "trial_opened"
        TRIAL_GENERATION_STARTED = "trial_generation_started"
        TRIAL_GENERATION_SUCCEEDED = "trial_generation_succeeded"
        TRIAL_GENERATION_FAILED = "trial_generation_failed"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    visitor_id = models.UUIDField(null=True)
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_events"
        indexes = [
            models.Index(
                fields=["user", "event_type", "created_at"],
                name="ue_user_event_created_idx",
            ),
            models.Index(
                fields=["visitor_id", "event_type", "created_at"],
                name="ue_visitor_event_created_idx",
            ),
            models.Index(
                fields=["event_type", "created_at"], name="ue_event_created_idx"
            ),
            models.Index(fields=["created_at"], name="ue_created_idx"),
        ]
