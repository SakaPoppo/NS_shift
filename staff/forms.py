from django import forms

from .models import StaffMember, StaffRegularDayOff


class StaffMemberForm(forms.ModelForm):
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
        self.fields["name"].widget.attrs.update(
            {
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
                "placeholder": "氏名を入力",
            }
        )
        self.fields["name"].help_text = "例：山田 花子"
        self.fields["job"].widget.attrs.update(
            {
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
            }
        )
        self.fields["role"].widget.attrs.update(
            {
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
            }
        )
        self.fields["ability_level"].widget.attrs.update(
            {
                "class": "mt-2 h-12 w-full rounded-xl border border-slate-200 bg-white px-4 text-sm outline-none transition focus:border-sky-700 focus:ring-2 focus:ring-sky-700/20",
            }
        )
        self.fields["ability_level"].help_text = (
            "1は自立前、3は新人指導可能、5は管理代行業務まで担える目安です。"
        )
        if self.instance and self.instance.pk:
            self.fields["regular_days_off"].initial = list(
                self.instance.regular_days_off.values_list("day_of_week", flat=True)
            )


StaffMemberCreateForm = StaffMemberForm
