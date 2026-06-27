from django.conf import settings #AUTH_USER_MODELを使うために必要
from django.db import models


class StaffMember(models.Model): #テーブル作成宣言
    class JobChoices(models.TextChoices): #DBに保存する値と、管理画面で表示する値を分けてる
        NURSE = "nurse", "看護師"
        CARE_WORKER = "care_worker", "介護士"

    class RoleChoices(models.TextChoices):
        LEADER = "leader", "リーダー"
        MEMBER = "member", "メンバー"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, #ユーザー情報を紐づける
        on_delete=models.CASCADE, #ユーザーが削除＝スタッフ情報も削除
        related_name="staff_members",  #user.staff_members.all()でユーザー情報からスタッフ情報を取得
    )
    name = models.CharField("氏名", max_length=100)
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