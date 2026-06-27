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
            name="StaffMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, verbose_name="氏名")),
                (
                    "job",
                    models.CharField(
                        blank=True,
                        choices=[("nurse", "看護師"), ("care_worker", "介護士")],
                        max_length=20,
                        verbose_name="職種",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("leader", "リーダー"), ("member", "メンバー")],
                        default="member",
                        max_length=20,
                        verbose_name="役割",
                    ),
                ),
                ("can_night_shift", models.BooleanField(default=True, verbose_name="夜勤可")),
                ("is_active", models.BooleanField(default=True, verbose_name="在籍中")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staff_members",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "staff_members",
                "ordering": ["id"],
            },
        ),
    ]
