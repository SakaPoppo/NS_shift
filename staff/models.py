from django.conf import settings #AUTH_USER_MODELを使うために必要
from django.db import models


class StaffMember(models.Model): #テーブル作成宣言
    class JobChoices(models.TextChoices): #DBに保存する値と、管理画面で表示する値を分けてる
        NURSE = "nurse", "看護師"
        CARE_WORKER = "care_worker", "介護士"

    class GenderChoices(models.TextChoices):
        MALE = "male", "男性"
        FEMALE = "female", "女性"

    class RoleChoices(models.TextChoices):
        LEADER = "leader", "リーダー"
        MEMBER = "member", "メンバー"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, #ユーザー情報を紐づける
        on_delete=models.CASCADE, #ユーザーが削除＝スタッフ情報も削除
        related_name="staff_members",  #user.staff_members.all()でユーザー情報からスタッフ情報を取得
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
    can_night_shift = models.BooleanField("夜勤可", default=True)
    is_active = models.BooleanField("在籍中", default=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        db_table = "staff_members" #ここが実際のテーブル名になる
        ordering = ["id"] #id順に並べる

    def __str__(self): #管理画面とかで名前をobject(1)みたいなのじゃなくて、ちゃんと名前を表示する
        return self.name
    
class StaffRegularDayOff(models.Model):
    class DayOfWeekChoices(models.IntegerChoices): #StaffRegularDayOff.DayOfWeekChoices.XXXで呼べる
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
        verbose_name="スタッフ",  # 管理画面での表示名
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
