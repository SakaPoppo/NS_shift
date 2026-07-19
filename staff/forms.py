from django import forms

from .models import StaffMember, StaffRegularDayOff


class StaffMemberForm(forms.ModelForm):
    """スタッフの基本情報と曜日固定休をまとめて扱うフォーム。

    regular_days_off は StaffMember の直接フィールドではなく、
    関連モデルの StaffRegularDayOff を更新するための入力欄として扱う。
    """

    gender = forms.ChoiceField(
        label="性別",
        choices=StaffMember.GenderChoices.choices,
        required=True,
        widget=forms.RadioSelect,
    )
    can_night_shift = forms.TypedChoiceField(
        label="夜勤の可否",
        choices=((True, "可"), (False, "不可")),
        coerce=lambda value: value in {True, "True", "true", "1", "on"},
        widget=forms.RadioSelect,
        initial=True,
    )
    regular_days_off = forms.TypedMultipleChoiceField(
        label="希望休日",
        choices=StaffRegularDayOff.DayOfWeekChoices.choices,
        coerce=int,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = StaffMember
        fields = ("name", "gender", "job", "role", "ability_level", "can_night_shift")
        widgets = {
            "gender": forms.RadioSelect,
            "job": forms.Select,
            "role": forms.Select,
            "ability_level": forms.Select,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["gender"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["can_night_shift"].widget.attrs.update({"class": "radio radio-primary radio-sm"})
        self.fields["regular_days_off"].widget.attrs.update({"class": "checkbox checkbox-primary checkbox-sm rounded-md"})
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
