from django.conf import settings
from django.db import models


class StaffMember(models.Model):
    """ユーザーごとに管理するスタッフ情報。

    シフト条件や勤務結果はこのモデルを起点に紐づくため、削除時は物理削除ではなく
    is_active による除外を使う設計になっている。
    """

    class JobChoices(models.TextChoices):
        NURSE = "nurse", "看護師"
        CARE_WORKER = "care_worker", "介護士"

    class GenderChoices(models.TextChoices):
        MALE = "male", "男性"
        FEMALE = "female", "女性"

    class RoleChoices(models.TextChoices):
        LEADER = "leader", "リーダー"
        MEMBER = "member", "メンバー"

    class AbilityLevelChoices(models.IntegerChoices):
        LEVEL_1 = 1, "1：1年目・自立なし"
        LEVEL_2 = 2, "2：基本業務が自立"
        LEVEL_3 = 3, "3：新人指導が可能"
        LEVEL_4 = 4, "4：重症患者対応が可能"
        LEVEL_5 = 5, "5：管理代行業務が可能"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_members",
    )
    name = models.CharField("氏名", max_length=100)
    gender = models.CharField(
        "性別",
        max_length=10,
        choices=GenderChoices.choices,
        blank=True,
    )
    job = models.CharField(
        "職種",
        max_length=20,
        choices=JobChoices.choices, #これで自由入力を防ぐ
        default=JobChoices.NURSE, #初期値は看護師
    )
    role = models.CharField(
        "役割",
        max_length=20,
        choices=RoleChoices.choices,
        default=RoleChoices.MEMBER,
    )
    ability_level = models.PositiveSmallIntegerField(
        "能力評価",
        choices=AbilityLevelChoices.choices,
        default=AbilityLevelChoices.LEVEL_2,
    )
    can_night_shift = models.BooleanField("夜勤可", default=True)
    is_holiday_off = models.BooleanField("祝日固定休", default=False)
    is_active = models.BooleanField("在籍中", default=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "staff_members"
        ordering = ["id"]

    def __str__(self):
        return self.name


class StaffRegularDayOff(models.Model):
    """毎週の曜日固定休。

    DayOffRequest が月ごとの希望休を表すのに対して、こちらは毎週繰り返す休みを表す。
    shifts 側では ShiftResult と混ぜず、基礎データとして別扱いする。
    """

    class DayOfWeekChoices(models.IntegerChoices):
        MONDAY = 0, "月"
        TUESDAY = 1, "火"
        WEDNESDAY = 2, "水"
        THURSDAY = 3, "木"
        FRIDAY = 4, "金"
        SATURDAY = 5, "土"
        SUNDAY = 6, "日"

    staff_member = models.ForeignKey(
        StaffMember,
        on_delete=models.CASCADE,
        related_name="regular_days_off",
        verbose_name="スタッフ",
    )
    day_of_week = models.IntegerField(
        "曜日",
        choices=DayOfWeekChoices.choices,
    )
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "staff_regular_days_off"
        ordering = ["staff_member_id", "day_of_week"]
        constraints = [
            models.UniqueConstraint(
                fields=["staff_member", "day_of_week"],
                name="unique_staff_regular_day_off",
            )
        ]

    def __str__(self):
        return f"{self.staff_member.name} - {self.get_day_of_week_display()}"
