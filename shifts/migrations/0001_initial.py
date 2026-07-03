from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ShiftPlan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("year", models.IntegerField(verbose_name="年")),
                ("month", models.IntegerField(verbose_name="月")),
                (
                    "title",
                    models.CharField(blank=True, max_length=255, verbose_name="タイトル"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "下書き"),
                            ("generated", "生成済み"),
                            ("confirmed", "確定"),
                        ],
                        default="draft",
                        max_length=20,
                        verbose_name="ステータス",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shift_plans",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "shift_plans",
                "ordering": ["-year", "-month", "-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="shiftplan",
            constraint=models.UniqueConstraint(
                fields=("user", "year", "month"),
                name="unique_shift_plan_user_year_month",
            ),
        ),
    ]
