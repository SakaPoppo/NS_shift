from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0003_staffregulardayoff"),
        ("shifts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShiftRule",
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
                ("off_days_per_staff", models.IntegerField(verbose_name="スタッフ1人あたりの月の休み数")),
                ("max_consecutive_work_days", models.IntegerField(verbose_name="最大連勤数")),
                ("night_shift_next_day_off", models.BooleanField(default=True, verbose_name="夜勤翌日は休みにする")),
                ("required_day_staff", models.IntegerField(default=0, verbose_name="必要日勤人数")),
                ("required_night_staff", models.IntegerField(default=0, verbose_name="必要夜勤人数")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "shift_plan",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shift_rule",
                        to="shifts.shiftplan",
                    ),
                ),
            ],
            options={
                "db_table": "shift_rules",
            },
        ),
        migrations.CreateModel(
            name="DayOffRequest",
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
                ("date", models.DateField(verbose_name="日付")),
                ("memo", models.TextField(blank=True, verbose_name="メモ")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "shift_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="day_off_requests",
                        to="shifts.shiftplan",
                    ),
                ),
                (
                    "staff_member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="day_off_requests",
                        to="staff.staffmember",
                    ),
                ),
            ],
            options={
                "db_table": "day_off_requests",
                "ordering": ["date", "staff_member_id"],
            },
        ),
        migrations.CreateModel(
            name="ShiftResult",
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
                ("date", models.DateField(verbose_name="日付")),
                (
                    "shift_type",
                    models.CharField(
                        choices=[
                            ("day", "日勤"),
                            ("night", "夜勤"),
                            ("after_night", "明け"),
                            ("off", "休み"),
                            ("off_request", "希望休"),
                        ],
                        max_length=20,
                        verbose_name="勤務区分",
                    ),
                ),
                (
                    "input_type",
                    models.CharField(
                        choices=[("manual", "手動"), ("generated", "自動生成")],
                        default="generated",
                        max_length=20,
                        verbose_name="入力種別",
                    ),
                ),
                ("is_locked", models.BooleanField(default=False, verbose_name="ロック済み")),
                ("memo", models.TextField(blank=True, verbose_name="メモ")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="作成日時")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新日時")),
                (
                    "shift_plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shift_results",
                        to="shifts.shiftplan",
                    ),
                ),
                (
                    "staff_member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="shift_results",
                        to="staff.staffmember",
                    ),
                ),
            ],
            options={
                "db_table": "shift_results",
                "ordering": ["date", "staff_member_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="dayoffrequest",
            constraint=models.UniqueConstraint(
                fields=("shift_plan", "staff_member", "date"),
                name="unique_day_off_request_shift_plan_staff_date",
            ),
        ),
        migrations.AddConstraint(
            model_name="shiftresult",
            constraint=models.UniqueConstraint(
                fields=("shift_plan", "staff_member", "date"),
                name="unique_shift_result_shift_plan_staff_date",
            ),
        ),
    ]
