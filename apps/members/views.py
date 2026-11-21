from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.shifts.services import reorder_members

from .models import EmploymentType, Member


@login_required
def member_list(request):
    qs = Member.objects.all()
    emp = request.GET.get("type", "")
    status = request.GET.get("status", "active")
    q = request.GET.get("q", "").strip()
    if emp in EmploymentType.values:
        qs = qs.filter(employment_type=emp)
    if status in Member.Status.values:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(member_no__icontains=q) | qs.filter(aliases__alias__icontains=q)
    return render(
        request,
        "members/list.html",
        {"members": qs.distinct(), "emp": emp, "status": status, "q": q, "emp_choices": EmploymentType.choices},
    )


@login_required
def order(request):
    if request.method == "POST":
        ids = [int(x) for x in request.POST.getlist("member") if x.isdigit()]
        if ids:
            reorder_members(ids, user=request.user)
            messages.success(request, "並び順を保存しました。")
        return redirect("members:order")
    members = Member.objects.filter(status=Member.Status.ACTIVE)
    return render(request, "members/order.html", {"members": members})
