from django import forms

from .models import StepUpScope


class StepUpForm(forms.Form):
    password = forms.CharField(label="パスワード", widget=forms.PasswordInput(attrs={"autofocus": True}))


class StepUpPasswordChangeForm(forms.Form):
    scope = forms.ChoiceField(label="対象機能", choices=StepUpScope.choices)
    new_password = forms.CharField(label="新しいパスワード", widget=forms.PasswordInput)
    new_password_confirm = forms.CharField(label="新しいパスワード（確認）", widget=forms.PasswordInput)
    note = forms.CharField(label="メモ（任意）", required=False, max_length=255)

    def clean(self):
        data = super().clean()
        if data.get("new_password") != data.get("new_password_confirm"):
            raise forms.ValidationError("確認用のパスワードが一致しません。")
        return data
