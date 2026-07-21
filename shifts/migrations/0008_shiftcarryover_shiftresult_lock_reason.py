import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shifts", "0007_remove_shiftplan_title"),
        ("staff", "0004_staffmember_ability_level_alter_staffmember_job"),
    ]

    operations = [
        migrations.AddField(
            model_name="shiftresult",
            name="lock_reason",
            field=models.CharField(blank=True, choices=[("month_boundary", "前月勤務の引き継ぎ"), ("user", "ユーザー固定")], default="", max_length=30, verbose_name="固定理由"),
        ),
        migrations.CreateModel(
            name="ShiftCarryover",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("previous_plan", "前月シフト表"), ("manual", "手入力")], max_length=30)),
                ("previous_last_shift_type", models.CharField(blank=True, choices=[("day", "日勤"), ("night", "夜勤"), ("after_night", "明け"), ("off", "休み"), ("off_request", "希望休"), ("paid_leave", "有給"), ("special_leave", "特別休暇"), ("training", "研修")], max_length=30, null=True)),
                ("previous_consecutive_work_days", models.PositiveIntegerField(default=0)),
                ("previous_shift_plan", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="next_month_carryovers", to="shifts.shiftplan")),
                ("shift_plan", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="carryovers", to="shifts.shiftplan")),
                ("staff_member", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shift_carryovers", to="staff.staffmember")),
            ],
            options={"db_table": "shift_carryovers"},
        ),
        migrations.AddConstraint(
            model_name="shiftcarryover",
            constraint=models.UniqueConstraint(fields=("shift_plan", "staff_member"), name="unique_shift_carryover_plan_staff"),
        ),
    ]
