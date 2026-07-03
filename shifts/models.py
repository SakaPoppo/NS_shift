from django.conf import settings
from django.db import models


class ShiftPlan(models.Model):
    class StatusChoices(models.TextChoices):
        DRAFT = "draft", "下書き"
        GENERATED = "generated", "生成済み"
        CONFIRMED = "confirmed", "確定"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shift_plans",
    )
    year = models.IntegerField("年")
    month = models.IntegerField("月")
    title = models.CharField("タイトル", max_length=255, blank=True)
    status = models.CharField(
        "ステータス",
        max_length=20,
        choices=StatusChoices.choices,
        default=StatusChoices.DRAFT,
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "shift_plans"
        ordering = ["-year", "-month", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "year", "month"],
                name="unique_shift_plan_user_year_month",
            )
        ]

    def __str__(self):
        return self.title or f"{self.year}年{self.month}月のシフト表"
