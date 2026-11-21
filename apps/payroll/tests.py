from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from apps.accounts import stepup
from apps.accounts.models import User
from apps.core.models import AuditLog, ImmutableAfterConfirmError
from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile
from apps.members.models import EmploymentType, Member
from apps.payroll import calc
from apps.payroll.models import (
    PayrollItemDefinition,
    PayrollLine,
    PayrollStatement,
    Product,
    RateTable,
    SalesCount,
    Team,
    TeamMembership,
)
from apps.payroll.pdf import render_pdf, slip_rows
from apps.shifts import services as shifts
from apps.shifts.models import WorkType

OCT, NOV = date(2026, 10, 1), date(2026, 11, 1)
C, I = EmploymentType.CONTRACTOR, EmploymentType.INTERN

# テスト用の架空の単価（区分の境界は既存シートと同じ、単価は架空）
C_DAYS, C_COUNTS = [0, 11, 16, 19, 22, 25, 28], [0, 40, 50, 60, 80, 100, 120]
I_DAYS, I_COUNTS = [0, 5, 8, 11, 16, 19, 22], [0, 24, 30, 40, 50, 60]


def fake_prices(rows, cols):
    return [[1000 * (r + 1) + 10 * c for c in range(cols)] for r in range(rows)]


def make_tables():
    RateTable.objects.create(name="個人事業主", employment_type=C, effective_from=date(2026, 1, 1),
                             day_lower_bounds=C_DAYS, count_lower_bounds=C_COUNTS, prices=fake_prices(7, 7))
    RateTable.objects.create(name="インターン", employment_type=I, effective_from=date(2026, 1, 1),
                             day_lower_bounds=I_DAYS, count_lower_bounds=I_COUNTS, prices=fake_prices(7, 6))


def work_days(member, month, n, half=0):
    wt = WorkType.objects.get(code="work")
    days = [month.replace(day=d) for d in range(1, n + 1)]
    shifts.set_shifts([member], days, wt)
    if half:
        shifts.set_shifts([member], [month.replace(day=d) for d in range(n + 1, n + 1 + half)], WorkType.objects.get(code="half"))


def count(member, month, kind, n, product=None):
    SalesCount.objects.create(member=member, month=month, kind=kind, count=n,
                              product=Product.objects.get(code=product) if product else None)


ACQ, CONF = SalesCount.Kind.ACQUIRED, SalesCount.Kind.CONFIRMED


class RateTableTests(TestCase):
    def setUp(self):
        self.table = RateTable(name="テスト", employment_type=C, effective_from=date(2026, 1, 1),
                               day_lower_bounds=[0, 11, 16], count_lower_bounds=[0, 40, 50],
                               prices=[[100, 110, 120], [200, 210, 220], [300, 310, 320]])
        self.table.full_clean()

    def test_boundaries_and_floor(self):
        self.assertEqual(self.table.lookup(10, 39), 100)
        self.assertEqual(self.table.lookup(11, 40), 210)
        self.assertEqual(self.table.lookup(16, 50), 320)
        self.assertEqual(self.table.lookup(Decimal("10.5"), 45), 110)  # 切り捨て

    def test_validation(self):
        self.table.prices = [[1, 2], [3, 4]]
        with self.assertRaises(ValidationError):
            self.table.full_clean()


class CalcTests(TestCase):
    def setUp(self):
        make_tables()
        self.user = User.objects.create_user("admin", password="x-pass-12345")
        self.c = Member.objects.create(member_no="C1", name="個人 一郎", employment_type=C, display_order=1)
        self.i = Member.objects.create(member_no="I1", name="インターン 一郎", employment_type=I, display_order=2)
        self.e = Member.objects.create(member_no="E1", name="社員 一郎", employment_type=EmploymentType.EMPLOYEE)

    def statement(self, member, month=NOV):
        calc.prepare_month(month)
        return PayrollStatement.objects.get(member=member, month=month)

    def line(self, st, code):
        return st.lines.get(code=code)

    def test_prepare_month_excludes_employees(self):
        self.assertEqual(calc.prepare_month(NOV), 2)
        self.assertFalse(PayrollStatement.objects.filter(member=self.e).exists())
        self.assertEqual(calc.ensure_month(NOV).payment_date, date(2026, 11, 30))  # 支払日は月末

    def test_contractor_standard_calculation(self):
        work_days(self.c, OCT, 19, half=1)  # 19.5日 → 19日として判定
        count(self.c, OCT, ACQ, 75)  # 前月獲得 → 60〜79件の列
        count(self.c, NOV, CONF, 50)
        st = calc.calculate(self.statement(self.c))
        self.assertEqual(st.status, PayrollStatement.Status.CALCULATED)
        unit = 1000 * 4 + 10 * 3  # 19〜21日の行・60〜79件の列
        sc = self.line(st, "sales_commission")
        self.assertEqual((sc.auto_amount, sc.detail), (50 * unit, f"50件 × {unit:,}円"))
        self.assertEqual(self.line(st, "app_fee").amount, 4000)
        self.assertEqual(self.line(st, "transport_contractor").amount, 0)
        self.assertEqual(st.net_amount, 50 * unit - 4000)
        self.assertFalse(st.lines.filter(code="transport_intern").exists())
        self.assertFalse(st.lines.filter(code="log_device").exists())  # 入社日未登録 → 自動では引かない

    def test_missing_previous_data_blocks_confirm_until_entered(self):
        count(self.c, NOV, CONF, 10)
        st = calc.calculate(self.statement(self.c))
        self.assertEqual(st.status, PayrollStatement.Status.IN_PROGRESS)
        self.assertTrue(st.blocking_errors)
        with self.assertRaises(ValueError):
            calc.confirm(st)
        with self.assertRaises(ValueError):  # 理由なしの修正は不可
            calc.set_input_override(st, "prev_attendance_days", 20, reason="")
        calc.set_input_override(st, "prev_attendance_days", 20, reason="前月はシステム導入前", user=self.user)
        st = calc.set_input_override(st, "prev_acquired_count", 45, reason="ダッシュボードより", user=self.user)
        self.assertEqual(st.status, PayrollStatement.Status.CALCULATED)
        self.assertEqual(self.line(st, "sales_commission").auto_amount, 10 * (4000 + 10))
        inp = st.inputs.get(key="prev_attendance_days")
        self.assertIsNone(inp.auto_value)  # 自動値と修正値は別々に保持
        self.assertEqual(inp.override_value, 20)
        self.assertTrue(AuditLog.objects.filter(action="payroll.input_override", reason="前月はシステム導入前").exists())

    def test_overrides_and_adhoc_survive_recalculation(self):
        work_days(self.i, OCT, 8)
        count(self.i, OCT, ACQ, 21)
        count(self.i, NOV, CONF, 23)
        st = calc.calculate(self.statement(self.i))
        self.assertEqual(self.line(st, "sales_commission").auto_amount, 23 * (3000 + 0))  # 8〜10日・〜23件
        allowance = self.line(st, "allowance")
        with self.assertRaises(ValueError):
            calc.set_line_override(allowance, 30000, reason="")
        calc.set_line_override(allowance, 30000, reason="特別対応", user=self.user)
        calc.add_adhoc_line(st, name="プロパンガス", kind="earning", amount=12000, detail="当月のみ", user=self.user)
        st = calc.calculate(st)
        self.assertEqual(self.line(st, "allowance").amount, 30000)
        self.assertTrue(st.lines.filter(is_adhoc=True, name="プロパンガス").exists())
        self.assertEqual(st.net_amount, 23 * 3000 + 30000 + 12000)
        self.assertFalse(st.lines.filter(code="app_fee").exists())  # アプリ手数料は個人事業主のみ

    def test_product_member_and_team_bonus(self):
        """個人別の商材報酬＋チーム達成ボーナス（架空データ）。"""
        leader = Member.objects.create(member_no="L1", name="リーダー", employment_type=C)
        a = Member.objects.create(member_no="A1", name="メンバーA", employment_type=C)
        b = Member.objects.create(member_no="B1", name="メンバーB", employment_type=C)
        PayrollItemDefinition.objects.get(code="sales_commission").excluded_members.add(leader)
        for code in ("propane_bath", "propane_nobath", "toshigas", "toshigas_denki", "houmon_denki"):
            PayrollItemDefinition.objects.get(code=f"product_{code}").members.add(leader)
        team = Team.objects.create(name="営業チーム", leader=leader, kind=Team.Kind.SALES)
        for m in (a, b):
            TeamMembership.objects.create(team=team, member=m, start_month=date(2026, 1, 1))
        for code, n in (("propane_bath", 1), ("propane_nobath", 0), ("toshigas", 30), ("toshigas_denki", 20), ("houmon_denki", 40)):
            count(leader, OCT, CONF, n, code)
        count(a, OCT, CONF, 60)
        count(b, OCT, CONF, 70)
        calc.prepare_month(OCT)
        st = PayrollStatement.objects.get(member=leader, month=OCT)
        st = calc.set_input_override(st, "unit_price", 6000, reason="電気の単価", user=self.user)
        self.assertFalse(st.lines.filter(code="sales_commission").exists())
        amounts = {l.code: l.amount for l in st.lines.all()}
        self.assertEqual(amounts["product_propane_bath"], 35000)
        self.assertEqual(amounts["product_propane_nobath"], 0)  # 25,000円 × 0件
        self.assertEqual(self.line(st, "product_propane_nobath").unit_price, 25000)
        self.assertEqual(amounts["product_toshigas"], 30 * 6000)  # 都市ガス＝電気と同じ単価
        self.assertEqual(amounts["product_toshigas_denki"], 20 * 6000)
        self.assertEqual(amounts["product_houmon_denki"], 40 * 6000)
        # 平均 (91 + 60 + 70) / 3 = 73.7件 → 3万円 × メンバー2名
        tb = self.line(st, "team_bonus")
        self.assertEqual(tb.amount, 60000)
        self.assertIn("73.7件", tb.detail)
        self.assertEqual(st.inputs.get(key="confirmed_count").value, 91)

    def test_team_bonus_manual_from_100(self):
        leader = Member.objects.create(member_no="L1", name="リーダー", employment_type=C)
        a = Member.objects.create(member_no="A1", name="メンバーA", employment_type=C)
        team = Team.objects.create(name="T", leader=leader)
        TeamMembership.objects.create(team=team, member=a, start_month=date(2026, 1, 1))
        count(leader, NOV, CONF, 110)
        count(a, NOV, CONF, 100)
        st = calc.calculate(self.statement(leader))
        self.assertTrue(any("別途協議" in m["text"] for m in st.blocking_errors))
        st = calc.set_line_override(self.line(st, "team_bonus"), 150000, reason="代表と協議", user=self.user)
        self.assertFalse(any("別途協議" in m["text"] for m in st.blocking_errors))

    def test_intern_leader_requires_4_days_this_month(self):
        team = Team.objects.create(name="インターン", leader=self.i, kind=Team.Kind.INTERN)
        others = [Member.objects.create(member_no=f"I{n}", name=f"インターン{n}", employment_type=I) for n in (2, 3)]
        for m, n in zip(others, (4, 7)):
            TeamMembership.objects.create(team=team, member=m, start_month=date(2026, 1, 1))
            count(m, NOV, CONF, n)
        work_days(self.i, NOV, 3)
        st = calc.calculate(self.statement(self.i))
        self.assertEqual(self.line(st, "intern_leader").amount, 0)
        self.assertIn("対象外", self.line(st, "intern_leader").detail)
        work_days(self.i, NOV, 4)
        st = calc.calculate(st)
        self.assertEqual(self.line(st, "intern_leader").amount, (4 + 7) * 500)

    def test_expenses_reflected_once_and_reopen(self):
        cat = ExpenseCategory.objects.get(code="transport")
        gas = ExpenseCategory.objects.get(code="gasoline")
        sf = SourceFile.objects.create(original_name="t.csv", sha256="x" * 64, file_format="csv", target_month=NOV)
        recs = [
            ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=self.c, auto_category=cat,
                                         auto_amount=30000, status="approved", dedupe_key="1"),
            ExpenseRecord.objects.create(source_file=sf, row_number=2, auto_member=self.c, auto_category=cat,
                                         auto_amount=20000, status="approved", dedupe_key="2"),
            ExpenseRecord.objects.create(source_file=sf, row_number=3, auto_member=self.c, auto_category=gas,
                                         auto_amount=8000, status="approved", dedupe_key="3"),
            ExpenseRecord.objects.create(source_file=sf, row_number=4, auto_member=self.c, auto_category=cat,
                                         auto_amount=99999, status="pending", dedupe_key="4"),  # 未承認は反映しない
        ]
        st = self.statement(self.c)
        calc.set_input_override(st, "prev_attendance_days", 20, reason="r")
        calc.set_input_override(st, "prev_acquired_count", 10, reason="r")
        st = calc.set_input_override(st, "confirmed_count", 1, reason="r")
        self.assertEqual(self.line(st, "transport_contractor").amount, 50000 - 35000)
        self.assertEqual(self.line(st, "transport_contractor").detail, "50,000円 − 35,000円")
        self.assertEqual(self.line(st, "gasoline").amount, 8000)
        st = calc.confirm(st, user=self.user)
        self.assertEqual(ExpenseRecord.objects.filter(status="reflected", reflected_statement=st).count(), 3)
        # 別の給与からは同じ経費を拾わない（二重反映の防止）
        other = PayrollStatement.objects.create(member=self.c, month=date(2026, 12, 1), employment_type=C)
        self.assertFalse(calc.calculate(other).lines.get(code="transport_contractor").source_refs)
        calc.reopen(st, reason="修正", user=self.user)
        self.assertEqual(ExpenseRecord.objects.filter(status="approved").count(), 3)
        recs[3].refresh_from_db()
        self.assertEqual(recs[3].status, "pending")

    def test_confirm_snapshot_pdf_and_immutability(self):
        work_days(self.c, OCT, 20)
        count(self.c, OCT, ACQ, 50)
        count(self.c, NOV, CONF, 30)
        st = calc.calculate(self.statement(self.c))
        st = calc.confirm(st, user=self.user)
        with self.assertRaises(ImmutableAfterConfirmError):  # 二重確定は不可
            calc.confirm(st)
        st.refresh_from_db()
        self.assertTrue(st.is_confirmed)
        net = st.net_amount
        snap = st.snapshot
        self.assertEqual(snap["totals"]["net"], net)
        self.assertEqual(snap["rate_table"]["name"], "個人事業主")
        # 単価表・項目を変えても、確定済みの給与とスナップショットは変わらない
        RateTable.objects.update(prices=fake_prices(7, 7)[::-1])
        PayrollItemDefinition.objects.filter(code="app_fee").update(default_amount=9999, name="変更後")
        with self.assertRaises(ImmutableAfterConfirmError):
            calc.calculate(st)
        st.refresh_from_db()
        self.assertEqual((st.net_amount, st.snapshot), (net, snap))
        with self.assertRaises(Exception), transaction.atomic():
            PayrollLine.objects.filter(statement=st).update(override_amount=1)
        pdf = render_pdf([(st.snapshot, False)])
        self.assertTrue(pdf.startswith(b"%PDF"))
        rows = slip_rows(st.snapshot)
        self.assertIn(("アプリ手数料", "個人事業主のみ", -4000), rows)
        self.assertIn("交通費精算", [r[0] for r in rows])  # 0円でも常に表示する項目
        self.assertNotIn("特別手当", [r[0] for r in rows])  # 0円の任意項目は表示しない
        self.assertEqual(sum(r[2] for r in rows), net)

    def test_first_payroll_log_device(self):
        self.c.joined_on = date(2026, 11, 5)
        self.c.save()
        count(self.c, NOV, CONF, 0)
        st = self.statement(self.c)
        calc.set_input_override(st, "prev_attendance_days", 0, reason="新規")
        st = calc.set_input_override(st, "prev_acquired_count", 0, reason="新規")
        self.assertEqual(self.line(st, "log_device").amount, 5000)
        calc.confirm(st)
        dec = PayrollStatement.objects.create(member=self.c, month=date(2026, 12, 1), employment_type=C)
        self.assertFalse(calc.calculate(dec).lines.filter(code="log_device").exists())


class PayrollViewTests(TestCase):
    def setUp(self):
        make_tables()
        self.user = User.objects.create_user("staff", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "dream-test")
        self.m = Member.objects.create(member_no="C1", name="青木 太郎", employment_type=C)
        self.client.force_login(self.user)

    def unlock(self):
        self.client.post("/stepup/payroll/", {"password": "dream-test"})

    def test_all_pages_require_stepup(self):
        calc.prepare_month(NOV)
        st = PayrollStatement.objects.get()
        for url in ["/payroll/", "/payroll/sales/", "/payroll/employees/", f"/payroll/{st.pk}/", f"/payroll/{st.pk}/pdf/",
                    "/payroll/pdf/?month=2026-11", "/payroll/export.csv?month=2026-11"]:
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.unlock()
        for url in ["/payroll/?month=2026-11", "/payroll/sales/?month=2026-11", "/payroll/employees/?month=2026-11",
                    f"/payroll/{st.pk}/", f"/payroll/{st.pk}/pdf/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertTrue(AuditLog.objects.filter(action="payroll.view").exists())  # 個人別給与の閲覧を記録

    def test_month_flow_and_export(self):
        self.unlock()
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "prepare"})
        st = PayrollStatement.objects.get()
        self.client.post("/payroll/sales/", {"month": "2026-11", "action": "save", f"conf_{self.m.pk}": "12", f"acq_{self.m.pk}": "40"})
        self.assertEqual(SalesCount.objects.get(member=self.m, kind=CONF).count, 12)
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st.refresh_from_db()
        self.assertEqual(st.status, PayrollStatement.Status.IN_PROGRESS)  # 前月データなし
        res = self.client.get("/payroll/export.csv?month=2026-11")
        text = res.content.decode("utf-8-sig")
        self.assertIn("青木 太郎", text)
        self.assertIn("差引支給額", text)
        self.assertEqual(self.client.get("/payroll/export.xlsx?month=2026-11").status_code, 200)

    def test_paste_parser(self):
        from apps.payroll.views import parse_counts_text

        Member.objects.create(member_no="C2", name="井上 次郎", employment_type=C)
        rows = parse_counts_text("■ 営業別件数\n営業\t2月\t合計\n青木太郎\t0\t4\t52\n井上 80件\n月合計\t1\t400\n不明 3")
        self.assertEqual([(r["name"], r["count"], r["member"].name if r["member"] else None) for r in rows],
                         [("青木太郎", 52, "青木 太郎"), ("井上", 80, "井上 次郎"), ("不明", 3, None)])

    def test_changing_counts_after_confirm_flags_review(self):
        self.unlock()
        calc.prepare_month(NOV)
        st = PayrollStatement.objects.get()
        count(self.m, NOV, CONF, 5)
        calc.set_input_override(st, "prev_attendance_days", 20, reason="r")
        st = calc.set_input_override(st, "prev_acquired_count", 20, reason="r")
        calc.confirm(st)
        self.client.post("/payroll/sales/", {"month": "2026-11", "action": "save", f"conf_{self.m.pk}": "6", f"acq_{self.m.pk}": ""})
        st.refresh_from_db()
        self.assertTrue(st.needs_review)
        self.assertEqual(st.snapshot["totals"]["net"], st.net_amount)  # 金額は変わらない
