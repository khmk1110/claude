"""初期設定ファイル（JSON）の取り込み。

本番の無料プランではサーバーでコマンドを実行できないため、メンバー・カード対応表・チーム・個人別の
給与項目をまとめて登録する手段として使う。実データを含むファイルはリポジトリに置かない。

形式（すべて任意）:
{
  "members": [
    {"name": "山田 太郎", "employment_type": "employee|contractor|intern", "member_no": "1",
     "joined_on": "2025-07-18", "is_agency": false, "paid_leave_target": true, "note": "役員",
     "aliases": ["山田"], "match": "旧表記（既存メンバーを探すときの名前。省略時は name）"},
    {"name": "退職 花子", "delete": true}
  ],
  "order": ["山田 太郎", "..."],                       # この順に並び順を付け直す
  "cards": [{"code": "0003", "member": "山田 太郎"}, {"code": "0001", "ignored": true, "label": "正社員"}],
  "teams": [{"name": "山田チーム", "kind": "sales|intern", "leader": "山田 太郎",
             "members": ["..."], "start_month": "2026-01"}],
  "items": [{"code": "sauna", "name": "サウナコンサル", "kind": "earning", "method": "manual",
             "scope": "member", "members": ["山田 太郎"], "display_order": 16}],
  "item_members": {"product_propane_bath": ["山田 太郎"]},   # 既存の個人専用項目に対象者を追加
  "item_excluded": {"sales_commission": ["山田 太郎"]}        # 既存の項目の対象外にする
}
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from apps.core import audit
from apps.core.months import parse_month
from apps.core.text import normalize_name
from apps.members.matching import match_member
from apps.members.models import CardCode, EmploymentType, Member, MemberAlias


class SetupError(Exception):
    pass


class _DryRun(Exception):
    pass


def _find(name: str, created: dict) -> Member:
    key = normalize_name(name)
    if key in created:
        return created[key]
    res = match_member(name)
    if not res.ok:
        cands = "、".join(c.name for c in res.candidates)
        raise SetupError(f"「{name}」のメンバーを特定できません" + (f"（候補: {cands}）" if cands else "（未登録）"))
    return res.member


def apply_setup(data: dict, *, user=None, dry_run: bool = True) -> list[str]:
    log: list[str] = []
    try:
        with transaction.atomic():
            _apply(data, user, log)
            if dry_run:
                raise _DryRun
    except _DryRun:
        log.append("（確認のみ。まだ登録していません）")
    return log


def _apply(data: dict, user, log: list[str]) -> None:
    from apps.payroll.models import PayrollItemDefinition, Team, TeamMembership

    created: dict[str, Member] = {}
    numbered: list[tuple[Member, str]] = []

    # --- メンバー ---
    for spec in data.get("members", []):
        name = spec["name"].strip()
        if spec.get("delete"):
            m = _find(spec.get("match", name), created)
            if m.has_history():
                raise SetupError(f"{m.name} は記録があるため削除できません（退職にしてください）")
            audit.log("member.delete", target=m, before={"name": m.name, "member_no": m.member_no}, user=user, reason="初期設定ファイル")
            m.delete()
            log.append(f"削除: {name}")
            continue
        res = match_member(spec.get("match", name))
        if res.ok:
            m, action = res.member, "更新"
        elif res.method == "ambiguous":
            raise SetupError(f"「{name}」に当てはまる人が複数います")
        else:
            m, action = Member(), "追加"
        before = {"name": m.name, "employment_type": m.employment_type, "joined_on": m.joined_on} if m.pk else None
        m.name = name
        if spec.get("employment_type"):
            if spec["employment_type"] not in EmploymentType.values:
                raise SetupError(f"{name}: 雇用区分が不正です")
            m.employment_type = spec["employment_type"]
        if not m.employment_type:
            raise SetupError(f"{name}: 雇用区分を指定してください")
        if "joined_on" in spec:
            m.joined_on = date.fromisoformat(spec["joined_on"]) if spec["joined_on"] else None
        for f in ("is_agency", "paid_leave_target", "note"):
            if f in spec:
                setattr(m, f, spec[f])
        if action == "追加" and "paid_leave_target" not in spec:
            m.paid_leave_target = m.employment_type == EmploymentType.EMPLOYEE
        m.save()
        created[normalize_name(name)] = m
        for alias in spec.get("aliases", []):
            MemberAlias.objects.get_or_create(member=m, alias=alias)
        if spec.get("member_no"):
            numbered.append((m, str(spec["member_no"])))
        audit.log("member.setup", target=m, before=before, after={"name": m.name, "employment_type": m.get_employment_type_display(),
                                                                  "joined_on": m.joined_on}, user=user, reason="初期設定ファイル")
        log.append(f"{action}: {name}（{m.get_employment_type_display()}）")

    # --- メンバー番号（重複しないよう一度仮番号にしてから付け直す） ---
    if numbered:
        wanted = [no for _, no in numbered]
        if len(set(wanted)) != len(wanted):
            raise SetupError("メンバー番号が重複しています")
        others = Member.objects.exclude(pk__in=[m.pk for m, _ in numbered]).filter(member_no__in=wanted)
        if others.exists():
            raise SetupError(f"番号 {', '.join(o.member_no for o in others)} は他のメンバーが使っています")
        for m, _ in numbered:
            Member.objects.filter(pk=m.pk).update(member_no=f"tmp-{m.pk}")
        for m, no in numbered:
            old = m.member_no
            Member.objects.filter(pk=m.pk).update(member_no=no)
            m.member_no = no
            if old != no:
                audit.log("member.renumber", target=m, before={"member_no": old}, after={"member_no": no}, user=user)
        log.append("メンバー番号: " + "、".join(f"{no} {m.name}" for m, no in sorted(numbered, key=lambda x: int(x[1]) if x[1].isdigit() else 0)))

    # --- 並び順 ---
    if data.get("order"):
        for i, name in enumerate(data["order"], start=1):
            m = _find(name, created)
            Member.objects.filter(pk=m.pk).update(display_order=i * 10)
        log.append(f"並び順: {len(data['order'])}人を指定の順に並べました")

    # --- カード対応表 ---
    for c in data.get("cards", []):
        member = _find(c["member"], created) if c.get("member") else None
        card, _ = CardCode.objects.update_or_create(
            code=c["code"], defaults={"member": member, "ignored": bool(c.get("ignored")), "label": c.get("label", ""),
                                      "is_active": c.get("is_active", True), "note": c.get("note", "")})
        audit.log("card.setup", target=card, after={"member": str(member or ""), "ignored": card.ignored}, user=user)
        log.append(f"カード〔{card.code}〕: {member or card.label or '-'}{'（給与に反映しない）' if card.ignored else ''}")

    # --- チーム ---
    for t in data.get("teams", []):
        leader = _find(t["leader"], created)
        team, _ = Team.objects.update_or_create(name=t["name"], defaults={"leader": leader, "kind": t.get("kind", "sales"), "is_active": True})
        start = parse_month(t.get("start_month") or date.today().strftime("%Y-%m"))
        names = []
        for name in t.get("members", []):
            m = _find(name, created)
            TeamMembership.objects.get_or_create(team=team, member=m, end_month=None, defaults={"start_month": start})
            names.append(m.name)
        audit.log("team.setup", target=team, after={"leader": leader.name, "members": names}, user=user)
        log.append(f"チーム「{team.name}」: リーダー {leader.name} / メンバー {'、'.join(names) or 'なし'}（{start:%Y年%m月}〜）")

    # --- 給与項目 ---
    for spec in data.get("items", []):
        item, _ = PayrollItemDefinition.objects.update_or_create(
            code=spec["code"],
            defaults={k: v for k, v in spec.items() if k in ("name", "kind", "method", "params", "default_amount", "scope",
                                                              "employment_types", "display_order", "slip_label", "show_on_slip")},
        )
        for name in spec.get("members", []):
            item.members.add(_find(name, created))
        log.append(f"給与項目「{item.name}」: {'、'.join(m.name for m in item.members.all()) or '全員/区分別'}")
    for code, names in (data.get("item_members") or {}).items():
        item = PayrollItemDefinition.objects.get(code=code)
        for name in names:
            item.members.add(_find(name, created))
        log.append(f"「{item.name}」の対象者: {'、'.join(names)}")
    for code, names in (data.get("item_excluded") or {}).items():
        item = PayrollItemDefinition.objects.get(code=code)
        for name in names:
            item.excluded_members.add(_find(name, created))
        log.append(f"「{item.name}」の対象外: {'、'.join(names)}")
    audit.log("setup.file", target_type="setup", after={"lines": len(log)}, user=user)
