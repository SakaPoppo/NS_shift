from django.conf import settings
from django.db import models

from staff.models import StaffMember


class ShiftPlan(models.Model):
    """ユーザーごとの月単位のシフト表。

    受け取るもの:
    - user: シフト表の所有者
    - year, month: 対象年月
    - status: 画面表示や進行状態に使う値

    返すもの:
    - DBに保存される ShiftPlan インスタンス
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
        # 管理画面やshellで見たときに、どの月のシフト表か分かる文字列を返す。
        return self.display_title


class ShiftRule(models.Model):
    """月全体に共通で適用するシフト条件。

    受け取るもの:
    - shift_plan: どのシフト表の条件か
    - 必要人数、休日日数、最大連勤数などの共通条件

    返すもの:
    - DBに保存される ShiftRule インスタンス
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
        # どのシフト表に紐づくルールかを文字列で返す。
        return f"{self.shift_plan} のルール"


class WeekdayShiftRule(models.Model):
    """曜日ごとに上書きする追加条件。

    受け取るもの:
    - shift_plan: 対象のシフト表
    - day_of_week: 0=月 〜 6=日
    - 必要人数や勤務レベル条件などの曜日別条件

    返すもの:
    - DBに保存される WeekdayShiftRule インスタンス
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
        # どの曜日の条件か分かる文字列を返す。
        return f"{self.shift_plan} - {self.get_day_of_week_display()}曜日"


class DateShiftRule(models.Model):
    """特定の日付だけに適用する追加条件。

    受け取るもの:
    - shift_plan: 対象のシフト表
    - target_date: 条件を上書きしたい日付
    - 必要人数や勤務レベル条件などの特定日条件

    返すもの:
    - DBに保存される DateShiftRule インスタンス
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
        # どの日付の条件か分かる文字列を返す。
        return f"{self.shift_plan} - {self.target_date}"


class DayOffRequest(models.Model):
    """スタッフごとの希望休。

    受け取るもの:
    - shift_plan: どのシフト表の希望休か
    - staff_member: 希望休を出したスタッフ
    - date, memo: 希望休の日付と補足

    返すもの:
    - DBに保存される DayOffRequest インスタンス
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
        # スタッフ名と日付が一目で分かる文字列を返す。
        return f"{self.staff_member.name} - {self.date}"


class ShiftResult(models.Model):
    """日ごとの最終的な勤務入力結果。

    受け取るもの:
    - shift_plan: どのシフト表の結果か
    - staff_member: 対象スタッフ
    - date, shift_type: その日の勤務内容
    - input_type, is_locked, memo: 入力種別や固定状態など

    返すもの:
    - DBに保存される ShiftResult インスタンス
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
        # スタッフ・日付・勤務区分をまとめた表示文字列を返す。
        return f"{self.staff_member.name} - {self.date} - {self.get_shift_type_display()}"
