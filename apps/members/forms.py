from django import forms

from .models import CardCode, Member


class MemberForm(forms.ModelForm):
    aliases = forms.CharField(
        label="別の書き方（任意）", required=False, widget=forms.Textarea(attrs={"rows": 2}),
        help_text="シフト表やLINEで使う書き方を1行に1つ（例: 甲口 ともあき、矢木）。取り込み時の名前の照合に使います。",
    )

    class Meta:
        model = Member
        fields = ["name", "name_kana", "short_name", "employment_type", "is_agency", "joined_on", "standard_days",
                  "paid_leave_target", "status", "retired_on", "note"]
        widgets = {"joined_on": forms.DateInput(attrs={"type": "date"}), "retired_on": forms.DateInput(attrs={"type": "date"}),
                   "note": forms.Textarea(attrs={"rows": 2})}
        help_texts = {"short_name": "シフト表で短く表示したいとき（空欄なら氏名）",
                      "paid_leave_target": "正社員は通常オン（役員はオフ）",
                      "standard_days": "空欄なら区分の初期値（正社員22日・個人事業主20日）"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["aliases"].initial = "\n".join(self.instance.aliases.values_list("alias", flat=True))

    def clean(self):
        data = super().clean()
        if data.get("status") == Member.Status.RETIRED and not data.get("retired_on"):
            self.add_error("retired_on", "退職にする場合は退職日を入力してください。")
        return data


class CardForm(forms.ModelForm):
    class Meta:
        model = CardCode
        fields = ["code", "member", "ignored", "label", "is_active"]
        labels = {"ignored": "給与に反映しない（正社員・代表など）"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = Member.objects.filter(status=Member.Status.ACTIVE)
        self.fields["member"].required = False
