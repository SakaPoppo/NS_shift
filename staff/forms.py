from django import forms
from django.forms import BaseModelFormSet, modelformset_factory

from .constants import MAX_ACTIVE_STAFF_COUNT
from .models import StaffMember, StaffRegularDayOff


BULK_STAFF_LEVELS = range(5, 0, -1)


class BulkStaffSetupForm(forms.Form):
    """一括登録するスタッフのLv別内訳を受け付けるフォーム。"""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for level in BULK_STAFF_LEVELS:
            self.fields[f"level_{level}_count"] = forms.IntegerField(
                label=f"Lv{level}の人数",
                min_value=0,
                initial=0,
            )
            self.fields[f"level_{level}_leader_count"] = forms.IntegerField(
                label=f"Lv{level}のリーダー数",
                min_value=0,
                initial=0,
            )
            self.fields[f"level_{level}_night_off_count"] = forms.IntegerField(
                label=f"Lv{level}の夜勤不可人数",
                min_value=0,
                initial=0,
            )

    def clean(self):
        cleaned_data = super().clean()
        total_count = 0

        for level in BULK_STAFF_LEVELS:
            count = cleaned_data.get(f"level_{level}_count")
            leader_count = cleaned_data.get(f"level_{level}_leader_count")
            night_off_count = cleaned_data.get(f"level_{level}_night_off_count")

            if count is not None:
                total_count += count
            if count is not None and leader_count is not None and leader_count > count:
                self.add_error(
                    f"level_{level}_leader_count",
                    f"Lv{level}のリーダー数は人数以下にしてください。",
                )
            if count is not None and night_off_count is not None and night_off_count > count:
                self.add_error(
                    f"level_{level}_night_off_count",
                    f"Lv{level}の夜勤不可人数は人数以下にしてください。",
                )

        if total_count == 0:
            raise forms.ValidationError("スタッフ数の合計は1人以上にしてください。")

        if self.user and (
            StaffMember.objects.filter(user=self.user, is_active=True).count() + total_count
            > MAX_ACTIVE_STAFF_COUNT
        ):
            raise forms.ValidationError(
                f"現在の在籍スタッフ数と今回の登録人数の合計は{MAX_ACTIVE_STAFF_COUNT}人以下にしてください。"
            )

        return cleaned_data

    def build_member_initial_data(self):
        """入力されたLv別の人数から、確認用フォームの初期値を生成する。"""
        if not self.is_valid():
            raise ValueError("有効な一括登録設定フォームからのみ初期値を生成できます。")

        initial_data = []
        staff_number = 1
        for level in BULK_STAFF_LEVELS:
            count = self.cleaned_data[f"level_{level}_count"]
            leader_count = self.cleaned_data[f"level_{level}_leader_count"]
            night_off_count = self.cleaned_data[f"level_{level}_night_off_count"]
            for index in range(count):
                initial_data.append(
                    {
                        "name": f"スタッフ{staff_number:02d}",
                        "gender": StaffMember.GenderChoices.FEMALE,
                        "job": StaffMember.JobChoices.NURSE,
                        "role": (
                            StaffMember.RoleChoices.LEADER
                            if index < leader_count
                            else StaffMember.RoleChoices.MEMBER
                        ),
                        "ability_level": level,
                        "can_night_shift": index >= night_off_count,
                        "regular_days_off": [],
                        "is_holiday_off": False,
                    }
                )
                staff_number += 1

        return initial_data


class StaffMemberForm(forms.ModelForm):
    """スタッフの基本情報と曜日固定休をまとめて扱うフォーム。

    regular_days_off は StaffMember の直接フィールドではなく、
    関連モデルの StaffRegularDayOff を更新するための入力欄として扱う。
    """

    gender = forms.ChoiceField(
        label="性別",
        choices=(
            (StaffMember.GenderChoices.FEMALE, "女性"),
            (StaffMember.GenderChoices.MALE, "男性"),
        ),
        required=True,
        widget=forms.RadioSelect,
        initial=StaffMember.GenderChoices.FEMALE,
    )
    can_night_shift = forms.TypedChoiceField(
        label="夜勤の可否",
        choices=((True, "可"), (False, "不可")),
        coerce=lambda value: value in {True, "True", "true", "1", "on"},
        required=True,
        widget=forms.RadioSelect,
        initial=True,
    )
    regular_days_off = forms.TypedMultipleChoiceField(
        label="固定休",
        choices=StaffRegularDayOff.DayOfWeekChoices.choices,
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = StaffMember
        fields = (
            "name", "gender", "job", "role", "ability_level",
            "can_night_shift", "is_holiday_off",
        )
        widgets = {
            "gender": forms.RadioSelect,
            "job": forms.Select,
            "role": forms.Select,
            "ability_level": forms.Select,
            "is_holiday_off": forms.CheckboxInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in ("job", "role", "ability_level", "can_night_shift"):
            self.fields[field_name].required = True
        self.fields["gender"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["can_night_shift"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["regular_days_off"].widget.attrs.update({"class": "checkbox checkbox-primary checkbox-sm rounded-md"})
        self.fields["is_holiday_off"].widget.attrs.update(
            {"class": "checkbox checkbox-primary checkbox-sm rounded-md"}
        )
        self.fields["name"].widget.attrs.update(
            {
                "class": "input input-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content placeholder:text-base-content/45 focus:border-brand-500 focus:outline-none",
                "placeholder": "氏名を入力",
            }
        )
        self.fields["name"].help_text = "例：山田 花子"
        self.fields["job"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["role"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["ability_level"].widget.attrs.update(
            {
                "class": "select select-bordered h-12 w-full rounded-lg border-base-300 bg-white text-sm text-base-content focus:border-brand-500 focus:outline-none",
            }
        )
        self.fields["ability_level"].help_text = (
            "1は自立前、3は新人指導可能、5は管理代行業務まで担える目安です。"
        )
        if self.instance and self.instance.pk:
            # 編集画面では、関連テーブルに保存済みの固定休をチェックボックスへ戻す。
            self.fields["regular_days_off"].initial = list(
                self.instance.regular_days_off.values_list("day_of_week", flat=True)
            )


StaffMemberCreateForm = StaffMemberForm


class BulkStaffMemberForm(StaffMemberForm):
    """一括登録・一括編集で再利用するスタッフ1人分のフォーム。"""


class BaseBulkStaffMemberFormSet(BaseModelFormSet):
    """フォーム1で決まった人数分だけ、未保存スタッフ用フォームを表示する。"""

    def __init__(self, *args, expected_form_count=0, **kwargs):
        self.expected_form_count = expected_form_count
        self.extra = expected_form_count
        super().__init__(*args, **kwargs)

    def _construct_form(self, index, **kwargs):
        form = super()._construct_form(index, **kwargs)
        # DB未保存のModelFormsetでは全行が extra form 扱いになるため、
        # フォーム1で生成した行は空欄でも無視されないよう必須検証を行う。
        if index < self.expected_form_count:
            form.empty_permitted = False
        return form

    def clean(self):
        super().clean()

        if self.total_form_count() != self.expected_form_count:
            raise forms.ValidationError(
                "登録対象のスタッフ数が不正です。最初からやり直してください。"
            )


BulkStaffMemberFormSet = modelformset_factory(
    StaffMember,
    form=BulkStaffMemberForm,
    formset=BaseBulkStaffMemberFormSet,
    extra=0,
    max_num=MAX_ACTIVE_STAFF_COUNT,
    validate_max=True,
)
