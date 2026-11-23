from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from . import stepup
from .forms import StepUpForm, StepUpPasswordChangeForm
from .models import StepUpPasswordHistory, StepUpScope

DEFAULT_NEXT = {StepUpScope.PAYROLL: "/payroll/", StepUpScope.EXPENSES: "/expenses/"}


@login_required
def stepup_view(request, scope):
    if scope not in StepUpScope.values:
        raise Http404
    next_url = request.POST.get("next") or request.GET.get("next") or DEFAULT_NEXT[scope]
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = DEFAULT_NEXT[scope]

    if stepup.is_verified(request, scope):
        return redirect(next_url)

    form = StepUpForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        result = stepup.verify(request, scope, form.cleaned_data["password"], remember_today=bool(request.POST.get("remember")))
        if result.ok:
            return redirect(next_url)
        error = result.message
    return render(
        request,
        "accounts/stepup.html",
        {"form": form, "scope_label": StepUpScope(scope).label, "next": next_url, "error": error},
        status=200 if not error else 403,
    )


@login_required
def stepup_password_change_view(request):
    if not (request.user.can_manage_stepup_passwords or stepup.is_verified(request, "payroll", touch=False)):
        raise PermissionDenied
    form = StepUpPasswordChangeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            stepup.set_password(
                form.cleaned_data["scope"],
                form.cleaned_data["new_password"],
                by=request.user,
                note=form.cleaned_data["note"],
            )
        except ValidationError as e:
            form.add_error("new_password", e)
        else:
            messages.success(request, "追加パスワードを変更しました。")
            return redirect("stepup_password_change")
    history = StepUpPasswordHistory.objects.select_related("changed_by")[:30]
    return render(request, "accounts/stepup_password_change.html", {"form": form, "history": history})


@login_required
def stepup_close_view(request, scope):
    """給与・経費の画面を閉じる（次に開くときはパスワードを再入力）。"""
    if request.method != "POST" or scope not in StepUpScope.values:
        raise Http404
    stepup.revoke(request, scope)
    from apps.core import audit

    audit.log("stepup.close", target_type="stepup", target_id=scope, target_repr=StepUpScope(scope).label, request=request)
    messages.success(request, f"{StepUpScope(scope).label}を閉じました。")
    return redirect("dashboard")



from django.contrib.auth import views as _auth_views


class OptionalLoginView(_auth_views.LoginView):
    """ログインは任意（シフトなどはログインなしで使える）。

    ブックマークなどで /login/ を開いても、ログイン画面を出さずにサイトへ進む。
    メニューの「ログイン」から開いた時（?login=1）だけログイン画面を出す。
    """

    template_name = "accounts/login.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("login") != "1":
            nxt = request.GET.get("next", "")
            ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            return redirect(nxt if nxt and ok else "/")
        return super().get(request, *args, **kwargs)
