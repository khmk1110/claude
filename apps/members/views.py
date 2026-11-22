from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from apps.core import audit

from apps.shifts.services import reorder_members

from .forms import CardForm, MemberForm
from .models import CardCode, EmploymentType, Member, MemberAlias


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


def _snapshot(m):
    return {"name": m.name, "employment_type": m.get_employment_type_display() if m.employment_type else "",
            "joined_on": m.joined_on, "status": m.get_status_display(), "retired_on": m.retired_on}


@login_required
def edit(request, pk=None):
    member = get_object_or_404(Member, pk=pk) if pk else Member()
    before = _snapshot(member) if pk else None
    form = MemberForm(request.POST or None, instance=member)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            m = form.save()
            wanted = [a.strip() for a in form.cleaned_data["aliases"].splitlines() if a.strip()]
            m.aliases.exclude(alias__in=wanted).delete()
            for a in wanted:
                MemberAlias.objects.get_or_create(member=m, alias=a)
            audit.log("member.update" if pk else "member.create", target=m, before=before, after=_snapshot(m), request=request)
        messages.success(request, f"{m.name}さんを{'更新' if pk else '追加'}しました（メンバー番号 {m.member_no}）。")
        return redirect("members:list")
    return render(request, "members/edit.html", {"form": form, "member": member,
                                                 "can_delete": bool(pk) and not member.has_history()})


@login_required
def delete(request, pk):
    member = get_object_or_404(Member, pk=pk)
    if request.method != "POST":
        return redirect("members:edit", pk=pk)
    if member.has_history():
        messages.error(request, "シフトや給与などの記録がある人は削除できません。在籍状況を「退職」にしてください。")
        return redirect("members:edit", pk=pk)
    audit.log("member.delete", target=member, before=_snapshot(member), request=request)
    name = member.name
    member.delete()
    messages.success(request, f"{name}さんを削除しました。")
    return redirect("members:list")


@login_required
def cards(request):
    instance = get_object_or_404(CardCode, pk=request.GET.get("edit")) if request.GET.get("edit") else None
    form = CardForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        card = form.save()
        audit.log("card.update", target=card, after={"member": str(card.member or ""), "ignored": card.ignored}, request=request)
        messages.success(request, f"カード〔{card.code}〕を保存しました。")
        return redirect("members:cards")
    return render(request, "members/cards.html", {"form": form, "cards": CardCode.objects.select_related("member"),
                                                  "editing": instance})
