"""給与・報酬の計算エンジン。

- 自動取得値（勤怠・営業件数・単価表・承認済み経費）と手動修正値は別々に保持する。
- 再計算しても手動修正値・当月のみの項目は残す。
- 確定済みの給与には一切触れない（ImmutableAfterConfirmError）。
- エラー（level=error）が残っている間は確定できない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance import services as attendance
from apps.core import audit
from apps.core.models import ImmutableAfterConfirmError
from apps.core.months import month_end, month_start, prev_month
from apps.members.models import EmploymentType, Member

from .models import (
    PayrollInput,
    PayrollItemDefinition,
    PayrollLine,
    PayrollMonth,
    PayrollStatement,
    RateTable,
    SalesCount,
    Team,
)

Key = PayrollInput.Key
Method = PayrollItemDefinition.Method
PAYROLL_TYPES = (EmploymentType.CONTRACTOR, EmploymentType.INTERN)  # 正社員は明細を作らない


def yen(n) -> str:
    return f"{int(n):,}円"


def num(v) -> str:
    if v is None:
        return "-"
    d = Decimal(v)
    return f"{d.normalize():f}" if d != d.to_integral() else f"{int(d):,}"


# ---------------------------------------------------------------------------
# 営業件数の取得
# ---------------------------------------------------------------------------
def is_product_member(member) -> bool:
    """商材別に報酬を計算する人（通常の「営業報酬」の対象外にしている人）。"""
    item = PayrollItemDefinition.objects.filter(code="sales_commission").first()
    return bool(item and item.excluded_members.filter(pk=member.pk).exists())


def has_sales(member, month) -> bool:
    """その月に契約（入金確定）の報酬があるか。ない月は交通費・経費だけの明細になる。"""
    return SalesCount.objects.filter(member=member, month=month, kind=SalesCount.Kind.CONFIRMED, count__gt=0).exists()


def sales_total(member, month, kind) -> int | None:
    """件数の合計。未入力なら None。

    - 獲得件数: 商材を指定しない行があればそれを、なければ商材別の合計
    - 入金確定件数: 通常の人は「商材を指定しない行」だけ（都市ガスなどの商材別の件数は別の行として計算するため
      二重に数えない）。商材別に報酬を計算する人は、商材別の件数の合計。
    """
    rows = list(SalesCount.objects.filter(member=member, month=month, kind=kind))
    if not rows:
        return None
    total_rows = [r for r in rows if r.product_id is None]
    if kind == SalesCount.Kind.CONFIRMED and not is_product_member(member):
        return total_rows[0].count if total_rows else None
    if total_rows and kind != SalesCount.Kind.CONFIRMED:
        return total_rows[0].count
    return sum(r.count for r in rows)


def product_count(member, month, product_code) -> int | None:
    row = SalesCount.objects.filter(
        member=member, month=month, kind=SalesCount.Kind.CONFIRMED, product__code=product_code
    ).first()
    return row.count if row else None


# ---------------------------------------------------------------------------
# 計算の文脈
# ---------------------------------------------------------------------------
@dataclass
class LineSpec:
    item: PayrollItemDefinition | None
    name: str
    kind: str
    auto_amount: int | None
    quantity: Decimal | None = None
    unit_price: int | None = None
    detail: str = ""
    sub_key: str = ""
    source_refs: list = field(default_factory=list)
    always_show: bool = False
    show_on_slip: bool = True
    display_order: int = 0


@dataclass
class Ctx:
    statement: PayrollStatement
    member: Member
    month: date
    inputs: dict
    rate_table: RateTable | None
    messages: list = field(default_factory=list)

    def value(self, key):
        inp = self.inputs.get(key)
        return inp.value if inp else None

    def error(self, text, item=""):
        self.messages.append({"level": "error", "text": text, "item": item})

    def warn(self, text, item=""):
        self.messages.append({"level": "warning", "text": text, "item": item})


# ---------------------------------------------------------------------------
# 元データ（PayrollInput）の自動取得
# ---------------------------------------------------------------------------
def refresh_inputs(st: PayrollStatement) -> dict:
    member, month = st.member, st.month
    pm = prev_month(month)
    prev_days, prev_src = attendance.actual_attendance_days(member, pm)
    cur_days, cur_src = attendance.actual_attendance_days(member, month)
    auto = {
        Key.PREV_ATTENDANCE_DAYS: (prev_days, prev_src),
        Key.PREV_ACQUIRED_COUNT: (sales_total(member, pm, SalesCount.Kind.ACQUIRED), f"営業件数（獲得）{pm:%Y年%m月}"),
        Key.CURRENT_ACQUIRED_COUNT: (sales_total(member, month, SalesCount.Kind.ACQUIRED), f"営業件数（獲得）{month:%Y年%m月}"),
        Key.CONFIRMED_COUNT: (sales_total(member, month, SalesCount.Kind.CONFIRMED), f"入金確定件数 {month:%Y年%m月}"),
        Key.CURRENT_ATTENDANCE_DAYS: (cur_days, cur_src),
    }
    inputs = {i.key: i for i in st.inputs.all()}
    for key, (val, src) in auto.items():
        inp = inputs.get(key) or PayrollInput(statement=st, key=key)
        inp.auto_value = val
        inp.auto_source = src if val is not None else "データなし（手入力してください）"
        inp.save()
        inputs[key] = inp

    # 単価は、前月出勤日数・前月獲得件数（修正値があればそれ）から判定
    table = RateTable.for_month(st.employment_type, month)
    days, count = inputs[Key.PREV_ATTENDANCE_DAYS].value, inputs[Key.PREV_ACQUIRED_COUNT].value
    unit = inputs.get(Key.UNIT_PRICE) or PayrollInput(statement=st, key=Key.UNIT_PRICE)
    if table and days is not None and count is not None:
        unit.auto_value = table.lookup(days, count)
        unit.auto_source = (
            f"単価表「{table.name}」前月出勤 {num(days)}日（切り捨て {RateTable.floor_days(days)}日）× 前月獲得 {num(count)}件"
        )
    else:
        unit.auto_value = None
        unit.auto_source = "単価表が未登録" if table is None else "前月出勤日数・前月獲得件数が必要"
    unit.save()
    inputs[Key.UNIT_PRICE] = unit
    return inputs


# ---------------------------------------------------------------------------
# 計算方法ごとの処理
# ---------------------------------------------------------------------------
def _unit_price(ctx: Ctx, item) -> int | None:
    v = ctx.value(Key.UNIT_PRICE)
    if v is None:
        if ctx.rate_table is None:
            ctx.error(f"{ctx.statement.get_employment_type_display()}の単価表が登録されていません。", item.code)
        else:
            ctx.error("前月出勤日数と前月獲得件数を入力してください（単価を判定できません）。", item.code)
        return None
    return int(v)


def h_sales_commission(ctx: Ctx, item) -> list[LineSpec]:
    count = ctx.value(Key.CONFIRMED_COUNT)
    if count is not None and int(count) == 0:
        # 入金確定が0件なら単価に関係なく0円（経費だけ支払う月など）
        return [LineSpec(item, item.name, item.kind, 0, Decimal(0), None, "")]
    unit = _unit_price(ctx, item)
    if count is None:
        ctx.error("当月の入金確定件数を入力してください。", item.code)
    if unit is None or count is None:
        return [LineSpec(item, item.name, item.kind, None)]
    count = int(count)
    return [LineSpec(item, item.name, item.kind, count * unit, Decimal(count), unit, f"{count}件 × {yen(unit)}")]


def h_product_commission(ctx: Ctx, item) -> list[LineSpec]:
    code = item.params.get("product")
    if item.params.get("for_others") and PayrollItemDefinition.objects.filter(
            is_active=True, scope=PayrollItemDefinition.Scope.MEMBER, method=item.method,
            params__product=code, members=ctx.member).exists():
        return []  # 個人専用の単価がある人（堀田さんなど）は、そちらで計算する
    count = product_count(ctx.member, ctx.month, code)
    if count is None:
        return []  # その月に件数の入力がない商材は行を作らない
    if item.params.get("unit_price") == "auto":
        unit = _unit_price(ctx, item)
    else:
        unit = item.default_amount
        if unit is None:
            ctx.error(f"「{item.name}」の単価が設定されていません。", item.code)
    if unit is None:
        return [LineSpec(item, item.name, item.kind, None, Decimal(count), None, f"{count}件")]
    return [LineSpec(item, item.name, item.kind, count * unit, Decimal(count), unit, f"{count}件 × {yen(unit)}")]


def h_fixed(ctx: Ctx, item) -> list[LineSpec]:
    if item.first_payroll_only and not _is_first_payroll(ctx, item):
        return []
    return [LineSpec(item, item.name, item.kind, item.default_amount or 0, detail=item.slip_label)]


def h_manual(ctx: Ctx, item) -> list[LineSpec]:
    return [LineSpec(item, item.name, item.kind, item.default_amount or 0, detail=item.slip_label)]


def h_unit_x_qty(ctx: Ctx, item) -> list[LineSpec]:
    return [LineSpec(item, item.name, item.kind, 0, detail=item.slip_label)]


def h_expense_sum(ctx: Ctx, item) -> list[LineSpec]:
    from apps.expenses.models import ExpenseRecord

    category = item.params.get("category")
    qs = ExpenseRecord.objects.filter(
        Q(status=ExpenseRecord.Status.APPROVED) | Q(status=ExpenseRecord.Status.REFLECTED, reflected_statement=ctx.statement)
    ).select_related("source_file", "member", "auto_member", "category", "auto_category")
    refs, total = [], 0
    for r in qs:
        m, c = r.effective_member, r.effective_category
        if m and m.pk == ctx.member.pk and c and c.code == category and r.effective_target_month == ctx.month:
            refs.append(r.pk)
            total += r.effective_amount or 0
    subtract = int(item.params.get("subtract") or 0)
    if ctx.member.pk in (item.params.get("no_subtract_members") or []):
        subtract = 0  # 設定で「交通費から引かない」にした人
    if subtract:
        amount = max(total - subtract, 0)
        if total and total < subtract:
            ctx.warn(f"{item.name}: 合計 {yen(total)} が差し引く額 {yen(subtract)} より少ないため 0円にしました。", item.code)
        detail = f"{yen(total)} − {yen(subtract)}" if refs else ""
    else:
        amount, detail = total, ""  # 立替金・ガソリン代などは件数を表示しない
    return [LineSpec(item, item.name, item.kind, amount, detail=detail, source_refs=refs)]


def h_team_bonus(ctx: Ctx, item) -> list[LineSpec]:
    teams = Team.objects.filter(leader=ctx.member, kind=Team.Kind.SALES, is_active=True)
    out = []
    for team in teams:
        members = list(team.members_for(ctx.month).exclude(pk=ctx.member.pk))
        if not members:
            continue
        counted = members + ([ctx.member] if item.params.get("include_leader_in_average", True) else [])
        counts, missing = [], []
        by_prev_acquired = item.params.get("count_basis") == "prev_acquired"
        for m in counted:
            if by_prev_acquired:
                # 前月の獲得件数（営業ダッシュボード）のチーム平均で判定する
                c = ctx.value(Key.PREV_ACQUIRED_COUNT) if m.pk == ctx.member.pk else sales_total(m, prev_month(ctx.month), SalesCount.Kind.ACQUIRED)
            else:
                c = ctx.value(Key.CONFIRMED_COUNT) if m.pk == ctx.member.pk else sales_total(m, ctx.month, SalesCount.Kind.CONFIRMED)
            (missing.append(m.name) if c is None else counts.append(int(c)))
        sub = f"team{team.pk}"
        basis = "前月獲得" if by_prev_acquired else "入金確定"
        if missing:
            ctx.error(f"チーム「{team.name}」: {', '.join(missing)} の{'前月の獲得件数' if by_prev_acquired else '入金確定件数'}が未入力です。", item.code)
            out.append(LineSpec(item, item.name, item.kind, None, sub_key=sub))
            continue
        avg = Decimal(sum(counts)) / len(counts)
        avg_txt = f"{avg.quantize(Decimal('0.1'))}"
        manual_from = item.params.get("manual_from")
        if manual_from is not None and avg >= manual_from:
            ctx.error(f"チーム「{team.name}」の{basis}平均が{avg_txt}件（{manual_from}件以上は別途協議）。金額を手入力してください。", item.code)
            out.append(LineSpec(item, item.name, item.kind, None, Decimal(len(members)), None, f"{basis} 平均{avg_txt}件（別途協議）", sub))
            continue
        per = 0
        for threshold, amount in sorted(item.params.get("tiers", [])):
            if avg >= threshold:
                per = amount
        detail = (f"{basis} 平均{avg_txt}件 → {yen(per)} × {len(members)}名" if per
                  else f"{basis} 平均{avg_txt}件（70件未満）")
        out.append(LineSpec(item, item.name, item.kind, per * len(members), Decimal(len(members)), per, detail, sub))
    return out  # リーダーでない人には項目を出さない（リーダーは0円でも行が出る）


def h_intern_leader(ctx: Ctx, item) -> list[LineSpec]:
    out = []
    for team in Team.objects.filter(leader=ctx.member, kind=Team.Kind.INTERN, is_active=True):
        members = list(team.members_for(ctx.month))
        if not item.params.get("include_leader"):
            members = [m for m in members if m.pk != ctx.member.pk]
        sub = f"team{team.pk}"
        key = Key.CURRENT_ATTENDANCE_DAYS if item.params.get("attendance_month", "current") == "current" else Key.PREV_ATTENDANCE_DAYS
        days = ctx.value(key)
        need = Decimal(str(item.params.get("min_attendance_days", 0)))
        if days is None:
            ctx.error("インターンリーダー手当: 出勤日数がわかりません（勤怠を入力するか手入力してください）。", item.code)
            out.append(LineSpec(item, item.name, item.kind, None, sub_key=sub))
            continue
        if Decimal(days) < need:
            out.append(LineSpec(item, item.name, item.kind, 0, detail=f"出勤{num(days)}日（{num(need)}日以上が条件）のため対象外", sub_key=sub))
            continue
        total, missing = 0, []
        for m in members:
            c = sales_total(m, ctx.month, SalesCount.Kind.CONFIRMED)
            (missing.append(m.name) if c is None else None)
            total += c or 0
        if missing:
            ctx.warn(f"インターンリーダー手当: {', '.join(missing)} の入金確定件数が未入力のため0件として計算しました。", item.code)
        rate = int(item.params.get("rate", 0))
        out.append(LineSpec(item, item.name, item.kind, total * rate, Decimal(total), rate, f"チーム{total}件 × {yen(rate)}", sub))
    return out


HANDLERS = {
    Method.SALES_COMMISSION: h_sales_commission,
    Method.PRODUCT_COMMISSION: h_product_commission,
    Method.FIXED: h_fixed,
    Method.MANUAL: h_manual,
    Method.UNIT_X_QTY: h_unit_x_qty,
    Method.EXPENSE_SUM: h_expense_sum,
    Method.TEAM_BONUS: h_team_bonus,
    Method.INTERN_LEADER: h_intern_leader,
}


def _is_first_payroll(ctx: Ctx, item) -> bool:
    """初回の給与か。過去に確定した給与がなく、入社（契約開始）が当月または前月の場合。"""
    if PayrollStatement.objects.filter(
        member=ctx.member, month__lt=ctx.month, status=PayrollStatement.Status.CONFIRMED
    ).exists():
        return False
    joined = ctx.member.joined_on
    if joined is None:
        ctx.warn(f"「{item.name}」: 入社日（契約開始日）が未登録のため、初回かどうか判定できません。必要なら手入力してください。", item.code)
        return False
    return prev_month(ctx.month) <= month_start(joined) <= ctx.month


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------
def _guard(st: PayrollStatement):
    if PayrollStatement.objects.filter(pk=st.pk, status=PayrollStatement.Status.CONFIRMED).exists():
        raise ImmutableAfterConfirmError("確定済みの給与は再計算できません。")


def ensure_month(month: date) -> PayrollMonth:
    pm, _ = PayrollMonth.objects.get_or_create(month=month, defaults={"payment_date": month_end(month)})
    return pm


@transaction.atomic
def payroll_members(month: date):
    """その月の給与の対象者。在籍中の人に加えて、退職した人でもその月の件数・経費がある人を入れる
    （契約の約2か月後に報酬を払うため、退職後に給与が出ることがある）。"""
    from apps.expenses.models import ExpenseRecord
    from apps.shifts.services import members_for_month

    month = month_start(month)
    ids = set(members_for_month(month).filter(employment_type__in=PAYROLL_TYPES).values_list("pk", flat=True))
    ids |= set(SalesCount.objects.filter(month=month, kind=SalesCount.Kind.CONFIRMED).values_list("member_id", flat=True))
    ids |= set(SalesCount.objects.filter(month=prev_month(month), kind=SalesCount.Kind.ACQUIRED).values_list("member_id", flat=True))
    recs = ExpenseRecord.objects.filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month)).exclude(status="excluded")
    ids |= {mid for pair in recs.values_list("member_id", "auto_member_id") for mid in pair if mid}
    return Member.objects.filter(pk__in=ids, employment_type__in=PAYROLL_TYPES).order_by("display_order", "id")


def paid_members(month: date):
    """その月に給与が出る人だけ（明細を作る人）。

    メンバーに入っていても、入りたてなどでその月に何もない人は明細を作らない（人数がそろわなくても進められる）。
    給与が出る＝入金確定の件数が1件以上・経費がある・この人専用の毎月の項目がある・チームのリーダー。
    """
    from apps.expenses.models import ExpenseRecord

    month = month_start(month)
    ids = set(SalesCount.objects.filter(month=month, kind=SalesCount.Kind.CONFIRMED, count__gt=0).values_list("member_id", flat=True))
    recs = ExpenseRecord.objects.filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month)).exclude(status="excluded")
    ids |= {mid for pair in recs.values_list("member_id", "auto_member_id") for mid in pair if mid}
    for item in PayrollItemDefinition.objects.filter(is_active=True, scope=PayrollItemDefinition.Scope.MEMBER,
                                                     method=PayrollItemDefinition.Method.FIXED).prefetch_related("members"):
        ids |= {m.pk for m in item.members.all() if item.applies_to(m, month)}
    for team in Team.objects.filter(is_active=True).select_related("leader"):
        if team.leader_id and team.members_for(month).exclude(pk=team.leader_id).exists():
            ids.add(team.leader_id)
    return Member.objects.filter(pk__in=ids, employment_type__in=PAYROLL_TYPES).order_by("display_order", "id")


def prepare_month(month: date, *, user=None, everyone: bool = False) -> int:
    """その月の給与データを作成する。

    ふだんは「その月に給与が出る人」だけ（paid_members）。everyone=True は在籍中の個人事業主・インターン全員分を作る。
    """
    month = month_start(month)
    ensure_month(month)
    n = 0
    keep = set()
    for m in (payroll_members(month) if everyone else paid_members(month)):
        keep.add(m.pk)
        _, created = PayrollStatement.objects.get_or_create(
            member=m, month=month, defaults={"employment_type": m.employment_type, "auto_created": not everyone})
        n += created
    if not everyone:
        # 自動で作った明細のうち、給与が出なくなった人（件数を消したなど）の分は消す。確定済み・手で直した明細は残す
        for st in (PayrollStatement.objects.filter(month=month, auto_created=True).exclude(member_id__in=keep)
                   .exclude(status=PayrollStatement.Status.CONFIRMED)):
            if not st.lines.filter(Q(is_adhoc=True) | Q(override_amount__isnull=False)).exists():
                st.delete()
    if n:
        audit.log("payroll.prepare_month", target_type="payroll_month", target_id=month.isoformat(),
                  target_repr=f"{month:%Y年%m月}", after={"created": n}, user=user)
    return n


def recompute_totals(st: PayrollStatement) -> None:
    earn = ded = 0
    for line in st.lines.all():
        if line.kind == PayrollItemDefinition.Kind.EARNING:
            earn += line.amount
        else:
            ded += line.amount
    st.earnings_total, st.deductions_total, st.net_amount = earn, ded, earn - ded


def refresh_month(month: date, *, user=None) -> int:
    """給与の画面を開いた時に呼ぶ。件数が入り始めた月は給与データを作り、もとのデータが変わった給与だけ計算し直す。

    「計算する」「全員を計算し直す」を押さなくても、いつも最新の金額になる（押し忘れて古い金額で確定するのを防ぐ）。
    確定済みの給与は変えない。
    """
    month = month_start(month)
    from apps.expenses.models import ExpenseRecord

    started = (PayrollStatement.objects.filter(month=month).exists()
               or ExpenseRecord.objects.filter(Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month),
                                               status__in=["approved", "reflected"]).exists()
               or SalesCount.objects.filter(month=month, kind=SalesCount.Kind.CONFIRMED).exists()
               or SalesCount.objects.filter(month=prev_month(month), kind=SalesCount.Kind.ACQUIRED).exists())
    if not started:
        return 0
    prepare_month(month, user=user)
    n = 0
    for st in PayrollStatement.objects.filter(month=month, stale=True).exclude(status=PayrollStatement.Status.CONFIRMED):
        calculate(st, user=user, log=False)
        n += 1
    return n


def refresh_for_expenses(month: date, *, user=None, member_ids=None) -> int:
    """経費が変わった人の明細を自動で計算し直す（確定前の明細だけ）。

    member_ids を渡すとその人たちを計算し直す（削除など、記録が残らない変更のため）。
    渡さない時は、前回の計算より後に承認・修正された経費がある人だけを計算し直す。
    """
    from apps.expenses.models import ExpenseRecord

    month = month_start(month)
    if PayrollStatement.objects.filter(month=month).exists():
        # 計算を始めた月なら、経費だけある人（退職した人など）の明細も作っておく
        have = set(PayrollStatement.objects.filter(month=month).values_list("member_id", flat=True))
        for m in paid_members(month):
            if m.pk not in have:
                PayrollStatement.objects.create(member=m, month=month, employment_type=m.employment_type)
    sts = PayrollStatement.objects.filter(month=month).exclude(status=PayrollStatement.Status.CONFIRMED).select_related("member")
    if member_ids is not None:
        sts = sts.filter(member_id__in=set(member_ids))
    n = 0
    for st in sts:
        if member_ids is None:
            recs = ExpenseRecord.objects.filter(Q(member=st.member) | Q(member__isnull=True, auto_member=st.member)).filter(
                Q(target_month=month) | Q(target_month__isnull=True, auto_target_month=month))
            if st.calculated_at:
                recs = recs.filter(updated_at__gt=st.calculated_at)
            if not recs.exists():
                continue
        calculate(st, user=user, log=False)
        n += 1
    return n


@transaction.atomic
def calculate(st: PayrollStatement, *, user=None, log: bool = True) -> PayrollStatement:
    _guard(st)
    st = PayrollStatement.objects.select_for_update().get(pk=st.pk)
    member, month = st.member, st.month
    if st.employment_type != member.employment_type:
        st.employment_type = member.employment_type
    inputs = refresh_inputs(st)
    ctx = Ctx(st, member, month, inputs, RateTable.for_month(st.employment_type, month))

    specs: list[LineSpec] = []
    items = PayrollItemDefinition.objects.filter(is_active=True).prefetch_related("members", "excluded_members")
    for item in items:
        if not item.applies_to(member, month):
            continue
        for spec in HANDLERS[item.method](ctx, item):
            spec.always_show = bool(item.params.get("always_show"))
            spec.show_on_slip = item.show_on_slip
            spec.display_order = item.display_order
            specs.append(spec)

    if is_product_member(member) and not SalesCount.objects.filter(
            member=member, month=month, kind=SalesCount.Kind.CONFIRMED, product__isnull=False).exists():
        # 商材別に報酬を計算する人は、商材別の件数がないと営業報酬が0円になってしまう
        ctx.error("商品別の件数（プロパン・都市ガス・訪問電気など）が入っていません。「1人ずつ確認」で入れてください。", "product")

    # 手で追加した項目・この人専用の毎月の項目に「チーム」の名前があれば、手入力のチームボーナスとみなす
    own_names = list(st.lines.filter(is_adhoc=True).values_list("name", flat=True))
    own_names += [x.name for x in specs if x.item and x.item.scope == PayrollItemDefinition.Scope.MEMBER
                  and x.item.method == PayrollItemDefinition.Method.FIXED]
    if any("チーム" in n for n in own_names):
        # 「チームボーナス手当」などを手で追加している時は、自動のチームリーダーボーナスは出さない（二重・要入力の表示を消す）
        specs = [x for x in specs if not (x.item and x.item.code == "team_bonus")]
        ctx.messages = [m for m in ctx.messages if m.get("item") != "team_bonus"]
    existing = {(l.item_id, l.sub_key): l for l in st.lines.filter(is_adhoc=False)}
    keep_ids = set()
    for spec in specs:
        key = (spec.item.pk if spec.item else None, spec.sub_key)
        line = existing.get(key) or PayrollLine(statement=st, item=spec.item, sub_key=spec.sub_key)
        line.code = spec.item.code if spec.item else ""
        line.name, line.kind = spec.name, spec.kind
        line.auto_amount, line.quantity, line.unit_price = spec.auto_amount, spec.quantity, spec.unit_price
        line.detail, line.source_refs = spec.detail, spec.source_refs
        line.always_show, line.show_on_slip, line.display_order = spec.always_show, spec.show_on_slip, spec.display_order
        line.save()  # 手動修正値（override_amount）はそのまま残す
        keep_ids.add(line.pk)
        if line.override_amount is not None:
            # 金額を手で直した行は、その行のエラー・注意を出さない（直したのに黄色い表示が残らないように）
            ctx.messages = [m for m in ctx.messages if m.get("item") != line.code]
    for line in existing.values():
        if line.pk not in keep_ids:
            line.delete()

    recompute_totals(st)
    dismissed = set((st.checks or {}).get("_dismissed") or [])
    if dismissed:  # 「対応済み」にした注意は出さない（ほかの方法で入れた時など）
        ctx.messages = [m for m in ctx.messages if m["text"] not in dismissed]
    st.calc_messages = ctx.messages
    st.status = PayrollStatement.Status.IN_PROGRESS if st.blocking_errors else PayrollStatement.Status.CALCULATED
    st.calculated_at = timezone.now()
    st.stale = False
    st.save()
    if log:
        audit.log("payroll.calculate", target=st, after={"earnings": st.earnings_total, "deductions": st.deductions_total,
                                                          "net": st.net_amount, "errors": len(st.blocking_errors)}, user=user)
    return st


@transaction.atomic
def set_input_override(st: PayrollStatement, key: str, value, *, reason: str, user=None) -> PayrollStatement:
    _guard(st)
    if value is not None and not reason:
        raise ValueError("修正理由を入力してください。")
    inp, _ = PayrollInput.objects.get_or_create(statement=st, key=key)
    before = {"auto": inp.auto_value, "override": inp.override_value}
    inp.override_value = None if value is None else Decimal(str(value))
    inp.override_reason = reason if value is not None else ""
    inp.updated_by = user
    inp.save()
    audit.log("payroll.input_override", target=st, before=before,
              after={"key": PayrollInput.Key(key).label, "override": inp.override_value}, reason=reason, user=user)
    return calculate(st, user=user, log=False)


@transaction.atomic
def set_line_override(line: PayrollLine, amount, *, reason: str, user=None) -> PayrollStatement:
    st = line.statement
    _guard(st)
    if amount is not None and not reason:
        raise ValueError("修正理由を入力してください。")
    before = {"name": line.name, "auto": line.auto_amount, "override": line.override_amount}
    line.override_amount = None if amount is None else int(amount)
    line.override_reason = reason if amount is not None else ""
    line.updated_by = user
    line.save()
    audit.log("payroll.line_override", target=st, before=before,
              after={"name": line.name, "override": line.override_amount}, reason=reason, user=user)
    return calculate(st, user=user, log=False)


@transaction.atomic
def add_adhoc_line(st: PayrollStatement, *, name: str, kind: str, amount: int, detail: str = "", reason: str = "", user=None):
    _guard(st)
    if not name or kind not in PayrollItemDefinition.Kind.values:
        raise ValueError("項目名と支給・控除を指定してください。")
    line = PayrollLine.objects.create(statement=st, name=name, kind=kind, auto_amount=None, override_amount=int(amount),
                                      override_reason=reason, detail=detail, is_adhoc=True, display_order=90, updated_by=user)
    audit.log("payroll.adhoc_add", target=st, after={"name": name, "kind": kind, "amount": int(amount)}, reason=reason, user=user)
    calculate(st, user=user, log=False)  # 手で足した項目に合わせて自動の項目・注意も見直す
    return line


@transaction.atomic
def remove_adhoc_line(line: PayrollLine, *, user=None, reason: str = ""):
    st = line.statement
    _guard(st)
    if not line.is_adhoc:
        raise ValueError("当月のみの項目だけ削除できます。")
    audit.log("payroll.adhoc_remove", target=st, before={"name": line.name, "amount": line.amount}, reason=reason, user=user)
    line.delete()
    calculate(st, user=user, log=False)


def issuer_info() -> dict:
    """明細書の発行者情報（設定画面で入力した値。なければサーバーの設定値）。"""
    from apps.core.models import SiteSetting

    return {
        "company": SiteSetting.get("issuer_company", settings.COMPANY_NAME),
        "representative": SiteSetting.get("issuer_representative", getattr(settings, "ISSUER_REPRESENTATIVE", "")),
        "address": SiteSetting.get("issuer_address", getattr(settings, "ISSUER_ADDRESS", "")),
        "tel": SiteSetting.get("issuer_tel", getattr(settings, "ISSUER_TEL", "")),
    }


def build_snapshot(st: PayrollStatement) -> dict:
    """明細PDF・過去分の再出力に必要な情報をすべて含める（後から設定が変わっても同じ明細を出せるように）。"""
    pm = ensure_month(st.month)
    table = RateTable.for_month(st.employment_type, st.month)
    return {
        "version": 1,
        "member": {"id": st.member_id, "name": st.member.name, "member_no": st.member.member_no,
                   "employment_type": st.employment_type, "employment_type_label": st.get_employment_type_display()},
        "month": st.month.isoformat(),
        "payment_date": pm.payment_date.isoformat(),
        "issuer": issuer_info(),
        "inputs": [
            {"key": i.key, "label": i.get_key_display(), "auto": None if i.auto_value is None else str(i.auto_value),
             "override": None if i.override_value is None else str(i.override_value),
             "value": None if i.value is None else str(i.value), "source": i.auto_source, "reason": i.override_reason}
            for i in st.inputs.all()
        ],
        "lines": [
            {"code": l.code, "name": l.name, "kind": l.kind, "quantity": None if l.quantity is None else str(l.quantity),
             "unit_price": l.unit_price, "auto_amount": l.auto_amount, "override_amount": l.override_amount,
             "amount": l.amount, "detail": l.detail, "override_reason": l.override_reason, "is_adhoc": l.is_adhoc,
             "show_on_slip": l.show_on_slip, "always_show": l.always_show, "source_refs": l.source_refs,
             "expense": bool(l.item_id and l.item.method == PayrollItemDefinition.Method.EXPENSE_SUM)}
            for l in st.lines.select_related("item")
        ],
        "rate_table": table.as_snapshot() if table else None,
        "totals": {"earnings": st.earnings_total, "deductions": st.deductions_total, "net": st.net_amount},
        "messages": st.calc_messages,
    }


@transaction.atomic
def confirm(st: PayrollStatement, *, user=None) -> PayrollStatement:
    from apps.expenses.models import ExpenseRecord

    _guard(st)
    st = PayrollStatement.objects.select_for_update().get(pk=st.pk)
    if st.status not in (PayrollStatement.Status.CALCULATED, PayrollStatement.Status.REVIEW):
        raise ValueError("計算済み（または確認待ち）の給与だけ確定できます。先に計算してください。")
    if st.blocking_errors:
        raise ValueError("エラーが残っているため確定できません。")
    recompute_totals(st)
    # 承認済み経費を「給与反映済み」にする（同じ経費を二重に反映しない）
    ref_ids = [rid for l in st.lines.all() for rid in (l.source_refs or [])]
    if ref_ids:
        updated = ExpenseRecord.objects.filter(pk__in=ref_ids, status=ExpenseRecord.Status.APPROVED).update(
            status=ExpenseRecord.Status.REFLECTED, reflected_statement=st, reflected_at=timezone.now()
        )
        already = ExpenseRecord.objects.filter(pk__in=ref_ids, status=ExpenseRecord.Status.REFLECTED,
                                               reflected_statement=st).count()
        if already != len(set(ref_ids)):
            raise ValueError("計算後に経費データの状態が変わりました。再計算してから確定してください。")
    st.snapshot = build_snapshot(st)
    st.status = PayrollStatement.Status.CONFIRMED
    st.confirmed_at = timezone.now()
    st.confirmed_by = user
    st.needs_review = False
    st.save()
    audit.log("payroll.confirm", target=st, after=st.snapshot["totals"], user=user)
    return st


@transaction.atomic
def reopen(st: PayrollStatement, *, user=None, reason: str) -> PayrollStatement:
    """確定の取り消し（理由必須）。確定時のスナップショットは監査ログに残す。"""
    from apps.expenses.models import ExpenseRecord

    if not reason:
        raise ValueError("確定を取り消す理由を入力してください。")
    st = PayrollStatement.objects.select_for_update().get(pk=st.pk)
    if not st.is_confirmed:
        raise ValueError("確定済みではありません。")
    audit.log("payroll.reopen", target=st, before=st.snapshot, reason=reason, user=user)
    st.status = PayrollStatement.Status.REVIEW
    st.save()  # 状態だけ変更（DB トリガーも許可）
    PayrollStatement.objects.filter(pk=st.pk).update(snapshot=None, confirmed_at=None, confirmed_by=None)
    ExpenseRecord.objects.filter(reflected_statement=st, status=ExpenseRecord.Status.REFLECTED).update(
        status=ExpenseRecord.Status.APPROVED, reflected_statement=None, reflected_at=None
    )
    st.refresh_from_db()
    return st


def mark_review(st: PayrollStatement, *, user=None) -> None:
    _guard(st)
    if st.status != PayrollStatement.Status.CALCULATED:
        raise ValueError("計算済みの給与だけ確認待ちにできます。")
    st.status = PayrollStatement.Status.REVIEW
    st.save(update_fields=["status", "updated_at"])
    audit.log("payroll.mark_review", target=st, user=user)
