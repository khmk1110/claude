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
        result = stepup.verify(request, scope, form.cleaned_data["password"])
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
    if not request.user.can_manage_stepup_passwords:
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
