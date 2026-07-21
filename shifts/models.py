from django.conf import settings
from django.db import models

from staff.models import StaffMember


class ShiftPlan(models.Model):
    """ユーザーごとに1か月単位で管理するシフト表。

    同一ユーザーは同じ年月のシフト表を1つしか持てない。
    画面表示名は保存せず、常に year と month から組み立てる。
    """

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

    @property
    def display_title(self):
        return f"{self.year}年{self.month}月 シフト表"

    def __str__(self):
        return self.display_title


class ShiftRule(models.Model):
    """月全体に共通で適用する基本条件。

    曜日条件や特定日条件は、このモデルの値を部分的に上書きする。
    """

    shift_plan = models.OneToOneField(
        ShiftPlan,
        on_delete=models.CASCADE,
        related_name="shift_rule",
    )
    off_days_per_staff = models.IntegerField("スタッフ1人あたりの月の休み数")
    max_consecutive_work_days = models.IntegerField("最大連勤数")
    night_shift_next_day_off = models.BooleanField(
        "夜勤翌日は休みにする",
        default=True,
    )
    required_day_staff = models.IntegerField("必要日勤人数", default=0)
    required_night_staff = models.IntegerField("必要夜勤人数", default=0)
    required_leader_staff = models.IntegerField("必要リーダー人数", default=0)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "shift_rules"

    def __str__(self):
        return f"{self.shift_plan} のルール"


class WeekdayShiftRule(models.Model):
    """曜日ごとに月共通条件を上書きする追加条件。

    同じシフト表の中では、1つの曜日に対して1レコードだけを持つ。
    人数系のフィールドで None を使うのは「0人にする」ではなく、
    この曜日では上書きせず月共通条件を使う、という意味。
    """

    class DayOfWeekChoices(models.IntegerChoices):
        MONDAY = 0, "月"
        TUESDAY = 1, "火"
        WEDNESDAY = 2, "水"
        THURSDAY = 3, "木"
        FRIDAY = 4, "金"
        SATURDAY = 5, "土"
        SUNDAY = 6, "日"

    shift_plan = models.ForeignKey(
        ShiftPlan,
        on_delete=models.CASCADE,
        related_name="weekday_rules",
    )
    day_of_week = models.IntegerField("曜日", choices=DayOfWeekChoices.choices)
    required_day_staff = models.IntegerField("必要日勤人数", null=True, blank=True)
    required_night_staff = models.IntegerField("必要夜勤人数", null=True, blank=True)
    required_leader_staff = models.IntegerField("必要リーダー人数", null=True, blank=True)
    min_ability_level = models.PositiveSmallIntegerField(
        "必要最低勤務レベル",
        choices=StaffMember.AbilityLevelChoices.choices,
        null=True,
        blank=True,
    )
    min_ability_level_staff_count = models.IntegerField("必要人数", null=True, blank=True)
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "weekday_shift_rules"
        ordering = ["shift_plan_id", "day_of_week"]
        constraints = [
            models.UniqueConstraint(
                fields=["shift_plan", "day_of_week"],
                name="unique_weekday_shift_rule_shift_plan_day_of_week",
            )
        ]

    def __str__(self):
        return f"{self.shift_plan} - {self.get_day_of_week_display()}曜日"


class DateShiftRule(models.Model):
    """特定の日付だけに適用する最優先の追加条件。

    条件の優先順位は「特定日条件 > 曜日条件 > 月共通条件」。
    ここでも None は「0」ではなく、下位条件へフォールバックすることを表す。
    """

    shift_plan = models.ForeignKey(
        ShiftPlan,
        on_delete=models.CASCADE,
        related_name="date_rules",
    )
    target_date = models.DateField("対象日")
    required_day_staff = models.IntegerField("必要日勤人数", null=True, blank=True)
    required_night_staff = models.IntegerField("必要夜勤人数", null=True, blank=True)
    required_leader_staff = models.IntegerField("必要リーダー人数", null=True, blank=True)
    min_ability_level = models.PositiveSmallIntegerField(
        "必要最低勤務レベル",
        choices=StaffMember.AbilityLevelChoices.choices,
        null=True,
        blank=True,
    )
    min_ability_level_staff_count = models.IntegerField("必要人数", null=True, blank=True)
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "date_shift_rules"
        ordering = ["target_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["shift_plan", "target_date"],
                name="unique_date_shift_rule_shift_plan_target_date",
            )
        ]

    def __str__(self):
        return f"{self.shift_plan} - {self.target_date}"


class DayOffRequest(models.Model):
    """スタッフが申請した希望休。

    ShiftResult とは別の基礎データで、編集画面では勤務結果より優先して表示する。
    """

    shift_plan = models.ForeignKey(
        ShiftPlan,
        on_delete=models.CASCADE,
        related_name="day_off_requests",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="day_off_requests",
    )
    date = models.DateField("日付")
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "day_off_requests"
        ordering = ["date", "staff_member_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["shift_plan", "staff_member", "date"],
                name="unique_day_off_request_shift_plan_staff_date",
            )
        ]

    def __str__(self):
        return f"{self.staff_member.name} - {self.date}"


class ShiftResult(models.Model):
    """日ごとの勤務結果。

    希望休や曜日固定休のような基礎データはこのモデルには保存しない。
    手入力結果と自動生成結果を input_type で区別し、再編集やリセットの対象を切り分ける。
    """

    class ShiftTypeChoices(models.TextChoices):
        DAY = "day", "日勤"
        NIGHT = "night", "夜勤"
        AFTER_NIGHT = "after_night", "明け"
        OFF = "off", "休み"
        OFF_REQUEST = "off_request", "希望休"
        PAID_LEAVE = "paid_leave", "有給"
        SPECIAL_LEAVE = "special_leave", "特別休暇"
        TRAINING = "training", "研修"

    class InputTypeChoices(models.TextChoices):
        MANUAL = "manual", "手動"
        GENERATED = "generated", "自動生成"

    class LockReasonChoices(models.TextChoices):
        MONTH_BOUNDARY = "month_boundary", "前月勤務の引き継ぎ"
        USER = "user", "ユーザー固定"

    shift_plan = models.ForeignKey(
        ShiftPlan,
        on_delete=models.CASCADE,
        related_name="shift_results",
    )
    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="shift_results",
    )
    date = models.DateField("日付")
    shift_type = models.CharField(
        "勤務区分",
        max_length=20,
        choices=ShiftTypeChoices.choices,
    )
    input_type = models.CharField(
        "入力種別",
        max_length=20,
        choices=InputTypeChoices.choices,
        default=InputTypeChoices.GENERATED,
    )
    is_locked = models.BooleanField("ロック済み", default=False)
    lock_reason = models.CharField(
        "固定理由",
        max_length=30,
        choices=LockReasonChoices.choices,
        blank=True,
        default="",
    )
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "shift_results"
        ordering = ["date", "staff_member_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["shift_plan", "staff_member", "date"],
                name="unique_shift_result_shift_plan_staff_date",
            )
        ]

    def __str__(self):
        return f"{self.staff_member.name} - {self.date} - {self.get_shift_type_display()}"


class ShiftCarryover(models.Model):
    """スタッフ単位の前月末勤務と、月初へ持ち越す連勤数。"""

    class SourceChoices(models.TextChoices):
        PREVIOUS_PLAN = "previous_plan", "前月シフト表"
        MANUAL = "manual", "手入力"

    shift_plan = models.ForeignKey(
        ShiftPlan, on_delete=models.CASCADE, related_name="carryovers"
    )
    staff_member = models.ForeignKey(
        StaffMember, on_delete=models.CASCADE, related_name="shift_carryovers"
    )
    source = models.CharField(max_length=30, choices=SourceChoices.choices)
    previous_shift_plan = models.ForeignKey(
        ShiftPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_month_carryovers",
    )
    previous_last_shift_type = models.CharField(
        max_length=30,
        choices=ShiftResult.ShiftTypeChoices.choices,
        null=True,
        blank=True,
    )
    previous_consecutive_work_days = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "shift_carryovers"
        constraints = [
            models.UniqueConstraint(
                fields=["shift_plan", "staff_member"],
                name="unique_shift_carryover_plan_staff",
            )
        ]
