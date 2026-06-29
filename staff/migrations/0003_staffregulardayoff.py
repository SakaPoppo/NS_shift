from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0002_staffmember_gender"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffRegularDayOff",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "day_of_week",
                    models.IntegerField(
                        choices=[
                            (0, "月"),
                            (1, "火"),
                            (2, "水"),
                            (3, "木"),
                            (4, "金"),
                            (5, "土"),
                            (6, "日"),
                        ],
                        verbose_name="曜日",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "staff_member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="regular_days_off",
                        to="staff.staffmember",
                        verbose_name="スタッフ",
                    ),
                ),
            ],
            options={
                "db_table": "staff_regular_days_off",
                "ordering": ["staff_member_id", "day_of_week"],
            },
        ),
        migrations.AddConstraint(
            model_name="staffregulardayoff",
            constraint=models.UniqueConstraint(
                fields=("staff_member", "day_of_week"),
                name="unique_staff_regular_day_off",
            ),
        ),
    ]
