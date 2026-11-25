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
    SalesEvidence,
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
        allowance = self.line(st, "special_allowance")  # 特別手当（手で金額を入れる項目）
        with self.assertRaises(ValueError):
            calc.set_line_override(allowance, 30000, reason="")
        calc.set_line_override(allowance, 30000, reason="特別対応", user=self.user)
        calc.add_adhoc_line(st, name="プロパンガス", kind="earning", amount=12000, detail="当月のみ", user=self.user)
        st = calc.calculate(st)
        self.assertEqual(self.line(st, "special_allowance").amount, 30000)
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
        # チームリーダーボーナスは前月（9月）の獲得件数のチーム平均で判定する
        for m, n in ((leader, 91), (a, 60), (b, 70)):
            count(m, date(2026, 9, 1), ACQ, n)
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
        count(leader, OCT, ACQ, 110)  # 前月の獲得件数で判定
        count(a, OCT, ACQ, 100)
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
        self.assertIn(("アプリ手数料", "", -4000), rows)
        self.assertIn("交通費精算", [r[0] for r in rows])  # 0円でも常に表示する項目
        self.assertNotIn("特別手当", [r[0] for r in rows])  # 0円の任意項目は表示しない
        self.assertEqual(sum(r[2] for r in rows), net)

    def test_log_device_item_removed(self):
        """運用変更によりログ機械代金の項目は削除済み（初回でも差し引かない）。"""
        self.assertFalse(PayrollItemDefinition.objects.filter(code="log_device").exists())
        self.c.joined_on = date(2026, 11, 5)
        self.c.save()
        count(self.c, NOV, CONF, 0)
        st = self.statement(self.c)
        calc.set_input_override(st, "prev_attendance_days", 0, reason="新規")
        st = calc.set_input_override(st, "prev_acquired_count", 0, reason="新規")
        self.assertFalse(st.lines.filter(name__contains="ログ").exists())

    def test_first_payroll_only_mechanism(self):
        """「初回の給与のみ」の仕組みは、管理者が新しく作った項目でも使える。"""
        PayrollItemDefinition.objects.create(code="welcome_kit", name="初回のみの控除（テスト）", kind="deduction",
                                             method="fixed", default_amount=1000, scope="employment_type",
                                             employment_types=[C], first_payroll_only=True)
        self.c.joined_on = date(2026, 11, 5)
        self.c.save()
        count(self.c, NOV, CONF, 0)
        st = self.statement(self.c)
        calc.set_input_override(st, "prev_attendance_days", 0, reason="新規")
        st = calc.set_input_override(st, "prev_acquired_count", 0, reason="新規")
        self.assertEqual(self.line(st, "welcome_kit").amount, 1000)
        calc.confirm(st)
        dec = PayrollStatement.objects.create(member=self.c, month=date(2026, 12, 1), employment_type=C)
        self.assertFalse(calc.calculate(dec).lines.filter(code="welcome_kit").exists())


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


class CommonProductTests(TestCase):
    """都市ガスは誰でも、その月に件数を入れたときだけ行が付く（通常の営業報酬とは二重に数えない）。"""

    def setUp(self):
        make_tables()
        self.m = Member.objects.create(member_no="C9", name="一般 太郎", employment_type=C)

    def calc_nov(self):
        calc.prepare_month(NOV)
        st = PayrollStatement.objects.get(member=self.m, month=NOV)
        calc.set_input_override(st, "prev_attendance_days", 20, reason="r")
        return calc.set_input_override(st, "prev_acquired_count", 45, reason="r")

    def test_toshigas_added_only_when_entered(self):
        count(self.m, NOV, CONF, 10)
        st = self.calc_nov()
        self.assertFalse(st.lines.filter(code="product_toshigas").exists())
        unit = int(st.inputs.get(key="unit_price").value)
        count(self.m, NOV, CONF, 3, "toshigas")
        st = calc.calculate(st)
        self.assertEqual(st.lines.get(code="product_toshigas").amount, 3 * unit)  # 都市ガス＝電気と同じ自動単価
        self.assertEqual(st.lines.get(code="sales_commission").amount, 10 * unit)  # 都市ガス分を二重に数えない
        SalesCount.objects.filter(product__code="toshigas").delete()  # その月だけ消す
        st = calc.calculate(st)
        self.assertFalse(st.lines.filter(code="product_toshigas").exists())

    def test_detail_page_can_add_and_remove_toshigas(self):
        user = User.objects.create_user("o", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        self.client.force_login(user)
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        count(self.m, NOV, CONF, 10)
        st = self.calc_nov()
        self.assertContains(self.client.get(f"/payroll/{st.pk}/"), "この月の商材")
        self.client.post(f"/payroll/{st.pk}/action/", {"action": "product_count", "product": "toshigas", "count": "2"})
        self.assertTrue(st.lines.filter(code="product_toshigas").exists())
        self.client.post(f"/payroll/{st.pk}/action/", {"action": "product_count", "product": "toshigas", "count": ""})
        self.assertFalse(st.lines.filter(code="product_toshigas").exists())
        self.assertContains(self.client.get("/payroll/sales/?month=2026-11"), "都市ガス")


DASHBOARD = """【10月 最終実績】
🔥 全体目標 500件 ｜ 実績 580件 🎉
⸻
【長谷＋田中・谷川】
72/60（進捗 120.0%）🎉
出勤 18/22日 ｜ AV 4.0
谷川
3日8件 ｜ AV 2.7
田中
3日4件 ｜ AV 1.3
⸻
【大石】
131/90（進捗 145.6%）🎉
出勤 20/20日 ｜ AV 6.6
⸻
【よしくん＋代表者】
94/60（進捗 156.7%）🎉
出勤 17/17日 ｜ AV 5.5
よしくん：67件
代表者：27件"""


class IngestTests(TestCase):
    def test_dashboard_blocks_and_team_split(self):
        from apps.payroll.ingest import parse_dashboard

        p = parse_dashboard(DASHBOARD)
        got = {e.name: (e.count, e.attendance) for e in p.entries}
        self.assertEqual(p.month, 10)
        self.assertEqual(got["谷川"], (8, 3.0))
        self.assertEqual(got["田中"], (4, 3.0))
        self.assertEqual(got["長谷"], (72 - 8 - 4, 18.0))  # 書かれていない1人はブロック合計から引く
        self.assertEqual(got["大石"], (131, 20.0))
        self.assertEqual(got["よしくん"][0], 67)
        self.assertEqual(got["代表者"][0], 27)

    def test_count_table_and_total(self):
        from apps.payroll.ingest import parse_count_table

        p = parse_count_table("■ 営業別件数\n営業\t2月\t3月\t合計\n大石一郎\t0\t4\t4\n長谷花子\t1\t2\t3\n月合計\t1\t6\t7")
        self.assertEqual([(e.name, e.count) for e in p.entries], [("大石一郎", 4), ("長谷花子", 3)])
        self.assertEqual(p.total, 7)

    def test_eneos_card_total_rule(self):
        from apps.expenses.parsers import card_totals_from_amounts

        codes = [(100, "0001"), (150, "0007"), (220, "0008"), (300, "0010")]
        amounts = [(100, 34521), (150, 77660), (165, 4389), (180, 82049), (220, 47243), (235, 700), (250, 47943),
                   (300, 37435), (330, 243086), (345, 4389), (360, 247475), (390, 700), (405, 202419)]
        totals, grand = card_totals_from_amounts(codes, amounts)
        # 「カード計」＝上の金額の合計と一致する金額。1行だけのカードはその金額
        self.assertEqual(totals, {"0001": 34521, "0007": 82049, "0008": 47943, "0010": 37435})
        self.assertEqual(grand, 247475)  # 最大の金額（この架空データでは合計と一致しない → 確認待ちになる）

    def test_gasoline_text(self):
        from apps.expenses.parsers import parse_gasoline_text

        d = parse_gasoline_text("0007 82049\n〔R0008〕 47,943円\nメモ")
        self.assertEqual([(r.card_code, r.amount) for r in d.rows], [("0007", 82049), ("0008", 47943)])


class CountsImportFlowTests(TestCase):
    def setUp(self):
        make_tables()
        self.user = User.objects.create_user("o", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        self.client.force_login(self.user)
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        self.oishi = Member.objects.create(name="大石 一郎", employment_type=C)
        self.yoshi = Member.objects.create(name="吉野 健", employment_type=C)
        self.tanaka = Member.objects.create(name="田中 次郎", employment_type=I)
        self.tanigawa = Member.objects.create(name="谷川 三郎", employment_type=I)
        self.hase = Member.objects.create(name="長谷 四郎", employment_type=EmploymentType.EMPLOYEE)

    def test_dashboard_import_with_alias_and_ignore(self):
        self.client.post("/payroll/counts/?month=2026-11", {"step": "parse", "kind": "acquired", "text": DASHBOARD})
        plan = self.client.session["counts_import"]
        self.assertEqual(plan["month"], "2026-10-01")  # 【10月 最終実績】→ 10月の獲得件数
        rows = {r["name"]: r for r in plan["rows"]}
        self.assertIsNone(rows["よしくん"]["member_id"])
        page = self.client.get("/payroll/counts/?month=2026-11")
        self.assertContains(page, "誰か選んでください")
        data = {"step": "apply"}
        for r in plan["rows"]:
            data[f"count_{r['i']}"] = r["count"]
            data[f"member_{r['i']}"] = r["member_id"] or "skip"
        data[f"member_{rows['よしくん']['i']}"] = self.yoshi.pk
        data[f"ignore_{rows['代表者']['i']}"] = "1"
        self.client.post("/payroll/counts/?month=2026-11", data)
        acq = {s.member.name: s.count for s in SalesCount.objects.filter(kind="acquired", month=OCT)}
        self.assertEqual(acq, {"大石 一郎": 131, "吉野 健": 67, "田中 次郎": 4, "谷川 三郎": 8, "長谷 四郎": 60})
        # 次回は「よしくん」は自動で特定、「代表者」は自動で取り込まない
        self.client.post("/payroll/counts/?month=2026-11", {"step": "parse", "kind": "acquired", "text": DASHBOARD})
        rows = {r["name"]: r for r in self.client.session["counts_import"]["rows"]}
        self.assertEqual(rows["よしくん"]["member_id"], self.yoshi.pk)
        self.assertTrue(rows["代表者"]["ignored"])

    def test_confirmed_table_total_check_and_month_home(self):
        text = "営業\t9月\t合計\n大石一郎\t1\t40\n吉野健\t0\t30\n月合計\t1\t70"
        self.client.post("/payroll/counts/?month=2026-11", {"step": "parse", "kind": "confirmed", "text": text})
        self.assertContains(self.client.get("/payroll/counts/?month=2026-11"), "一致しました")
        page = self.client.get("/payroll/?month=2026-11")
        self.assertContains(page, "明細を確認して確定")


def _table_png(rows):
    """スプレッドシートの表のスクショに似せた画像（架空の名前）。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 26)
    img = Image.new("RGB", (900, 60 + 48 * len(rows)), "white")
    d = ImageDraw.Draw(img)
    for i, row in enumerate(rows):
        y = 30 + 48 * i
        d.line((20, y - 8, 880, y - 8), fill=(210, 210, 210))
        d.text((20, y), row[0], font=font, fill="black")
        for j, v in enumerate(row[1:]):
            d.text((360 + 130 * j, y), str(v), font=font, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class TableScreenshotTests(TestCase):
    def setUp(self):
        make_tables()
        self.user = User.objects.create_user("o", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        self.client.force_login(self.user)
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        self.a = Member.objects.create(name="大石 一郎", employment_type=C)
        self.b = Member.objects.create(name="吉野 健太", employment_type=C)
        self.c = Member.objects.create(name="長谷川 花子", employment_type=C)

    def test_guess_member_tolerates_one_wrong_char(self):
        from apps.payroll.ingest import guess_member

        cands = [(1, "大石一郎"), (2, "吉野健太"), (3, "長谷川花子")]
        self.assertEqual(guess_member("吉野健大", cands), 2)
        self.assertEqual(guess_member("長谷川花", cands), 3)
        self.assertIsNone(guess_member("山田", cands))

    def test_screenshot_reads_names_and_counts_then_waits_and_confirms(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        png = _table_png([("営業", "9月", "合計"), ("大石一郎", 3, 41), ("吉野健太", 0, 27), ("長谷川花子", 2, 12), ("月合計", 5, 80)])
        res = self.client.post("/payroll/counts/?month=2026-11", {
            "step": "parse", "kind": "confirmed", "file": SimpleUploadedFile("sheet.png", png, content_type="image/png")})
        self.assertIn("ev=", res["Location"])  # 待ち画面へ（テストでは読み取りはその場で終わる）
        res = self.client.get(res["Location"])
        self.assertEqual(res.status_code, 302)
        plan = self.client.session["counts_import"]
        got = {r["member_id"]: r["count"] for r in plan["rows"]}
        self.assertEqual(got, {self.a.pk: 41, self.b.pk: 27, self.c.pk: 12})
        self.assertEqual(plan["total"], 80)
        self.assertContains(self.client.get("/payroll/counts/?month=2026-11"), "一致しました")

    def test_wait_page_while_processing(self):
        ev = SalesEvidence.objects.create(month=NOV, kind="confirmed", status="processing")
        res = self.client.get(f"/payroll/counts/?month=2026-11&ev={ev.pk}")
        self.assertContains(res, "読み取っています")


class MultiScreenshotAndExpensesInPayrollTests(TableScreenshotTests):
    def test_two_screenshots_overlapping_rows_are_merged(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        p1 = _table_png([("営業", "9月", "合計"), ("大石一郎", 3, 41), ("吉野健太", 0, 27)])
        p2 = _table_png([("吉野健太", 0, 27), ("長谷川花子", 2, 12), ("月合計", 5, 80)])
        res = self.client.post("/payroll/counts/?month=2026-11", {"step": "parse", "kind": "confirmed", "file": [
            SimpleUploadedFile("a.png", p1, content_type="image/png"), SimpleUploadedFile("b.png", p2, content_type="image/png")]})
        self.client.get(res["Location"])
        plan = self.client.session["counts_import"]
        self.assertEqual({r["member_id"]: r["count"] for r in plan["rows"]}, {self.a.pk: 41, self.b.pk: 27, self.c.pk: 12})
        self.assertEqual(len(plan["rows"]), 3)
        self.assertEqual(plan["total"], 80)

    def test_expenses_from_payroll_page_with_payroll_password_only(self):
        from apps.members.models import CardCode

        CardCode.objects.create(member=self.a, code="0007")
        page = self.client.get("/payroll/?month=2026-11")
        self.assertContains(page, "/payroll/expenses/upload/")
        self.assertNotContains(page, "経費を開く（パスワード）")
        res = self.client.post("/payroll/expenses/gasoline-text/", {"text": "0007 82049", "target_month": "2026-11",
                                                                     "next": "/payroll/?month=2026-11"})
        self.assertEqual(res["Location"], "/payroll/?month=2026-11")
        rec = ExpenseRecord.objects.get()
        self.assertEqual(rec.effective_amount, 82049)
        page = self.client.get("/payroll/?month=2026-11")
        self.assertContains(page, "まとめて承認（1件）")
        self.assertContains(page, "82,049")
        self.client.post("/payroll/expenses/approve-all/", {"month": "2026-11", "group": "gasoline"})
        rec.refresh_from_db()
        self.assertEqual(rec.status, "approved")
        # 経費も給与のパスワードで開ける
        self.assertEqual(self.client.get("/expenses/").status_code, 200)


class DashboardOcrRobustnessTests(TestCase):
    def test_brackets_and_plus_misread(self):
        from apps.payroll.ingest import parse_dashboard

        text = "[9 月最終実績]\n|長谷十田中谷川|\n・\n72/60( 進捗 120.0%)\n出勤 18/22 日\n谷川\n3 日 8 件\n田中\n3日4件\nxX\n大石\n131/90( 進捗 145.6%)\n出勤 20/20 日"
        p = parse_dashboard(text, ["長谷", "田中", "谷川", "大石"])
        self.assertEqual({e.name: e.count for e in p.entries}, {"谷川": 8, "田中": 4, "長谷": 60, "大石": 131})


def _ranking_png(rows):
    """「月別ランキング」の画面に似せた画像（架空の名前。名前は縦書き、獲得計は薄い字）。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    W = 1206
    f = ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 36)
    img = Image.new("RGB", (W, 900 + 300 * len(rows) + 200), "white")
    d = ImageDraw.Draw(img)
    d.text((74, 700), "総合ランキング", font=f, fill="black")
    d.text((560, 700), "月別ランキング", font=f, fill="black")
    d.text((960, 700), "デイリー", font=f, fill="black")
    for i, (name, pt, acq) in enumerate(rows):
        y = 1100 + 300 * i
        d.line((70, y - 150, 1130, y - 150), fill=(225, 225, 225), width=2)
        d.text((120, y - 18), str(i + 1), font=f, fill="black")
        for j, ch in enumerate(name):
            d.text((316, y - len(name) * 23 + j * 46), ch, font=f, fill="black")
        d.text((477, y - 90), "関東", font=ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 28), fill="black")
        d.text((675, y - 18), f"{pt}pt", font=f, fill=(200, 120, 0))
        d.text((900, y - 18), str(acq), font=f, fill=(215, 222, 230))
        d.text((1075, y - 18), str(acq - 3), font=f, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class RankingScreenshotTests(TestCase):
    def test_vertical_names_and_faint_counts(self):
        from datetime import date as _date

        from apps.payroll import ingest
        from apps.payroll.views import build_counts_plan, _known_names

        a = Member.objects.create(name="大石 一郎", employment_type=C)
        b = Member.objects.create(name="吉野 健太", employment_type=C)
        c = Member.objects.create(name="長谷川 花子", employment_type=C)
        png = _ranking_png([("大石一郎", 125, 115), ("長谷川花子", 70, 68), ("吉野健太", 21, 21)])

        class F:
            name = "rank.png"

            @staticmethod
            def read():
                return png

        text = ingest.text_from_upload(F, "acquired", _known_names()[2])
        plan = build_counts_plan("acquired", text, _date(2026, 11, 1))
        self.assertEqual(plan["month"], "2026-10-01")  # 月の記載がないので前月の獲得件数
        self.assertEqual({r["member_id"]: r["count"] for r in plan["rows"]}, {a.pk: 115, c.pk: 68, b.pk: 21})


class TransportPanelTests(TableScreenshotTests):
    def test_transport_panel_shows_minus_35000(self):
        from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile

        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="t.pdf", sha256="t" * 64, file_format="manual", document_kind="transport",
                                       target_month=NOV, status="parsed")
        ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=self.a, auto_category=cat, auto_amount=52000,
                                     auto_target_month=NOV, dedupe_key="t1")
        page = self.client.get("/payroll/?month=2026-11")
        self.assertContains(page, "52,000 − 35,000")
        self.assertContains(page, "17,000")
        self.assertContains(page, "0 − 35,000")  # 交通費がない個人事業主も表示（0円）


class ExpenseRedirectTests(TableScreenshotTests):
    def test_upload_errors_return_to_payroll_page(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        data = {"kind": "transport", "target_month": "2026-11", "next": "/payroll/?month=2026-11#transport"}
        csv = "名前,金額\n大石一郎,52000\n".encode()
        r1 = self.client.post("/payroll/expenses/upload/", {**data, "files": SimpleUploadedFile("t.csv", csv)})
        self.assertEqual(r1["Location"], "/payroll/?month=2026-11#transport")
        # 同じファイルをもう一度（重複エラー）でも給与の画面に戻る
        r2 = self.client.post("/payroll/expenses/upload/", {**data, "files": SimpleUploadedFile("t.csv", csv)})
        self.assertEqual(r2["Location"], "/payroll/?month=2026-11#transport")
        r3 = self.client.post("/payroll/expenses/line/", {"category": "999999", "target_month": "", "text": "x",
                                                          "next": "/payroll/?month=2026-11#transport"})
        self.assertIn(r3.status_code, (302, 404))


class ExpensesFixPageTests(TableScreenshotTests):
    def test_fix_name_amount_exclude_add_and_approve(self):
        from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile
        from apps.members.models import MemberAlias

        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="t.png", sha256="f" * 64, file_format="image", document_kind="transport",
                                       target_month=NOV, status="parsed")
        unknown = ExpenseRecord.objects.create(source_file=sf, row_number=1, name_text="大右", auto_category=cat, auto_amount=52080,
                                               auto_target_month=NOV, dedupe_key="f1")
        wrong_amt = ExpenseRecord.objects.create(source_file=sf, row_number=2, name_text="吉野", auto_member=self.b, auto_category=cat,
                                                 auto_amount=999, auto_target_month=NOV, dedupe_key="f2")
        boss = ExpenseRecord.objects.create(source_file=sf, row_number=3, name_text="代表", auto_category=cat, auto_amount=10000,
                                            auto_target_month=NOV, dedupe_key="f3")
        page = self.client.get("/payroll/expenses/fix/?month=2026-11&group=transport")
        self.assertContains(page, "大右")
        self.assertContains(page, "直す必要がある行が")
        res = self.client.post("/payroll/expenses/fix/", {
            "month": "2026-11", "group": "transport", "then": "approve",
            f"member_{unknown.pk}": self.a.pk, f"amount_{unknown.pk}": "52,080", f"alias_{unknown.pk}": "1",
            f"member_{wrong_amt.pk}": self.b.pk, f"amount_{wrong_amt.pk}": "6822",
            f"member_{boss.pk}": "", f"amount_{boss.pk}": "10000", f"exclude_{boss.pk}": "1",
            "add_member": self.c.pk, "add_amount": "3000",
        })
        self.assertEqual(res["Location"], "/payroll/?month=2026-11#transport")
        unknown.refresh_from_db(); wrong_amt.refresh_from_db(); boss.refresh_from_db()
        self.assertEqual((unknown.effective_member, unknown.status), (self.a, "approved"))
        self.assertEqual((wrong_amt.effective_amount, wrong_amt.status), (6822, "approved"))
        self.assertEqual(boss.status, "excluded")
        self.assertTrue(MemberAlias.objects.filter(member=self.a, alias="大右").exists())
        added = ExpenseRecord.objects.get(source_file=sf, row_number=4)
        self.assertEqual((added.effective_member, added.effective_amount, added.effective_target_month), (self.c, 3000, NOV))
        self.assertContains(self.client.get("/payroll/?month=2026-11"), "52,080 − 35,000")


class ApproveWithOcrWarningsTests(TableScreenshotTests):
    def test_ok_button_confirms_low_confidence_but_not_missing_person(self):
        from apps.expenses.models import ExpenseCategory, ExpenseIssue, ExpenseRecord, SourceFile

        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="o.png", sha256="o" * 64, file_format="image", document_kind="transport",
                                       target_month=NOV, status="parsed")
        good = ExpenseRecord.objects.create(source_file=sf, row_number=1, name_text="大石", auto_member=self.a, auto_category=cat,
                                            auto_amount=52080, auto_target_month=NOV, dedupe_key="o1")
        nobody = ExpenseRecord.objects.create(source_file=sf, row_number=2, name_text="中沢", auto_category=cat, auto_amount=7184,
                                              auto_target_month=NOV, dedupe_key="o2")
        for r in (good, nobody):
            ExpenseIssue.objects.create(record=r, source_file=sf, code="low_confidence", message="画像から読み取りました")
        self.client.post("/payroll/expenses/approve-all/", {"month": "2026-11", "group": "transport"})
        good.refresh_from_db(); nobody.refresh_from_db()
        self.assertEqual(good.status, "approved")
        self.assertNotEqual(nobody.status, "approved")
        # 承認できなかった理由は名前で表示する
        page = self.client.get("/payroll/?month=2026-11")
        self.assertNotContains(page, "2行目")



class ReviewPageTests(TableScreenshotTests):
    def _prepare(self):
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "prepare"})
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        return list(PayrollStatement.objects.filter(month=NOV).order_by("member__display_order", "member_id"))

    def test_preview_adhoc_and_confirm_next(self):
        sts = self._prepare()
        self.assertTrue(sts)
        st = sts[0]
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertContains(page, "明 細 書")
        self.assertContains(page, st.member.name)
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "adhoc_add", "name": "サウナコンサル", "kind": "earning", "amount": "30,000"})
        self.assertContains(self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}"), "サウナコンサル")

    def test_product_member_needs_product_counts(self):
        from apps.payroll.models import PayrollItemDefinition, Product

        sc = PayrollItemDefinition.objects.get(code="sales_commission")
        sc.excluded_members.add(self.a)
        prod = Product.objects.create(code="visit_elec", name="訪問電気")
        item = PayrollItemDefinition.objects.create(code="product_visit_elec", name="訪問電気", kind="earning",
                                                    method=PayrollItemDefinition.Method.PRODUCT_COMMISSION,
                                                    params={"product": "visit_elec"}, default_amount=7250,
                                                    scope=PayrollItemDefinition.Scope.MEMBER, display_order=12)
        item.members.add(self.a)
        sts = self._prepare()
        st = next(x for x in sts if x.member_id == self.a.pk)
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertContains(page, "商品別の件数（プロパン・都市ガス・訪問電気など）が入っていません")
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "products", f"count_{prod.pk}": "3"})
        st.refresh_from_db()
        line = st.lines.get(code="product_visit_elec")
        self.assertEqual(line.amount, 3 * 7250)
        self.assertFalse(any("商品別の件数" in m["text"] for m in st.calc_messages))


class IssuerAndSlipOrderTests(TableScreenshotTests):
    def test_issuer_setting_and_row_order(self):
        from apps.core.models import SiteSetting

        self.client.post("/settings/", {"action": "issuer", "issuer_company": "架空株式会社", "issuer_representative": "山田 太郎",
                                     "issuer_address": "東京都架空区1-2-3", "issuer_tel": "000-0000-0000"})
        self.assertEqual(SiteSetting.get("issuer_representative"), "山田 太郎")
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.filter(month=NOV, member=self.a).first()
        self.assertIsNotNone(st)  # 「全員を計算し直す」で、まだない人の分も作られる
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertContains(page, "山田 太郎")
        html = page.content.decode()
        html = html[html.index('class="slip-table"'):]
        order = [html.index(x) for x in ("営業報酬", "交通費精算", "ガソリン代", "アプリ手数料")]  # チームリーダーボーナスはリーダーだけ
        self.assertEqual(order, sorted(order))
        self.assertNotIn("<b>手当</b>", html)
        pdf = self.client.get(f"/payroll/{st.pk}/pdf/")
        self.assertEqual(pdf["Content-Type"], "application/pdf")


class ExpenseOnlySlipTests(TableScreenshotTests):
    def test_intern_with_only_transport(self):
        from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile
        from apps.payroll.pdf import slip_rows

        intern = Member.objects.create(name="谷川 三郎", employment_type=I)
        SalesCount.objects.create(member=intern, month=NOV, kind="confirmed", count=0)
        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="e.png", sha256="e" * 64, file_format="image", document_kind="transport",
                                       target_month=NOV, status="parsed")
        ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=intern, auto_category=cat, auto_amount=8702,
                                     auto_target_month=NOV, dedupe_key="e1", status="approved")
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=intern, month=NOV)
        self.assertFalse(st.blocking_errors)  # 入金確定0件なら単価がなくても0円
        snap = calc.build_snapshot(st)
        self.assertEqual([(n, a) for n, _, a in slip_rows(snap)], [("交通費精算", 8702)])  # 経費の行だけ
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertNotContains(page, "報酬額合計")


class AppFeeAndOrderTests(TableScreenshotTests):
    def test_app_fee_only_chosen_until_october(self):
        fee = PayrollItemDefinition.objects.get(code="app_fee")
        self.assertEqual(fee.params.get("only_members_until"), "2026-10")
        self.client.post("/payroll/settings/app-fee/", {f"m_{self.a.pk}": "1"})
        self.assertEqual(list(fee.members.all()), [self.a])
        self.assertTrue(fee.applies_to(self.a, OCT))
        self.assertFalse(fee.applies_to(self.b, OCT))  # 10月分は選んだ人だけ
        self.assertTrue(fee.applies_to(self.b, NOV))   # 11月分からは個人事業主全員

    def test_deductions_last_and_no_count_detail(self):
        from apps.payroll.pdf import slip_rows

        snap = {"lines": [
            {"name": "営業報酬", "kind": "earning", "amount": 1000, "always_show": True},
            {"name": "ガソリン代", "kind": "deduction", "amount": 500, "always_show": True, "expense": True},
            {"name": "立替金", "kind": "earning", "amount": 300, "expense": True},
            {"name": "アプリ手数料", "kind": "deduction", "amount": 4000, "always_show": True},
        ]}
        self.assertEqual([r[0] for r in slip_rows(snap)], ["営業報酬", "立替金", "ガソリン代", "アプリ手数料"])


class ReviewBaseInputsTests(TableScreenshotTests):
    def test_enter_prev_counts_and_attendance(self):
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        self.assertTrue(st.blocking_errors)  # 前月の出勤・獲得がない
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertContains(page, "計算のもとの数字")
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "base", "prev_attendance_days": "20",
                                                        "prev_acquired_count": "65", "confirmed_count": "40"})
        st.refresh_from_db()
        self.assertEqual(SalesCount.objects.get(member=self.a, month=OCT, kind="acquired").count, 65)  # 件数のデータとして保存
        self.assertEqual(SalesCount.objects.get(member=self.a, month=NOV, kind="confirmed", product__isnull=True).count, 40)
        unit = st.inputs.get(key="unit_price").value
        self.assertIsNotNone(unit)
        self.assertEqual(st.lines.get(code="sales_commission").amount, 40 * int(unit))


class SlipDownloadTests(TableScreenshotTests):
    def test_individual_and_zip_names(self):
        import io
        import zipfile

        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.c, month=NOV)
        res = self.client.get(f"/payroll/{st.pk}/pdf/?download=1")
        self.assertIn("attachment", res["Content-Disposition"])
        self.assertIn("11%E6%9C%88%E5%88%86", res["Content-Disposition"])  # 「11月分」（URLエンコード）
        # 確定分を ZIP で（1人1つのPDF）
        for s_ in PayrollStatement.objects.filter(month=NOV):
            PayrollStatement.objects.filter(pk=s_.pk).update(status="confirmed", snapshot=calc.build_snapshot(s_))
        z = self.client.get("/payroll/pdf/?month=2026-11")
        self.assertEqual(z["Content-Type"], "application/zip")
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        self.assertIn("11月分明細書_長谷川花子.pdf", names)
        self.assertEqual(len(names), PayrollStatement.objects.filter(month=NOV).count())


class SlipNoDetailTests(TestCase):
    def test_details_hidden_on_slip(self):
        from apps.payroll.pdf import slip_rows

        snap = {"lines": [
            {"code": "sales_commission", "name": "営業報酬", "kind": "earning", "amount": 4000, "detail": "1件 × 4,000円"},
            {"code": "transport_contractor", "name": "交通費精算", "kind": "earning", "amount": 6690, "detail": "41,690円 − 35,000円"},
            {"code": "team_bonus", "name": "チームリーダーボーナス", "kind": "earning", "amount": 80000, "detail": "前月獲得 平均85.0件"},
            {"code": "app_fee", "name": "アプリ手数料", "kind": "deduction", "amount": 4000, "detail": "個人事業主のみ"},
        ]}
        rows = {n: d for n, d, _ in slip_rows(snap)}
        self.assertEqual(rows["営業報酬"], "1件 × 4,000円")
        self.assertEqual(rows["交通費精算"], "")
        self.assertEqual(rows["チームリーダーボーナス"], "")
        self.assertEqual(rows["アプリ手数料"], "")


class SimpleCountLinesTests(TestCase):
    def test_name_colon_count_lines(self):
        from apps.payroll.ingest import parse_dashboard

        p = parse_dashboard("大石：60件\n長谷：4件\n吉野 8\n合計：72件")
        self.assertEqual([(e.name, e.count) for e in p.entries], [("大石", 60), ("長谷", 4), ("吉野", 8)])
        self.assertEqual(p.warnings, [])


class TransfersAndChecksTests(TableScreenshotTests):
    def test_transfer_checks_and_csv(self):
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.c, month=NOV)
        PayrollStatement.objects.filter(pk=st.pk).update(status="confirmed", snapshot=calc.build_snapshot(st))
        page = self.client.get("/payroll/transfers/?month=2026-11")
        self.assertContains(page, self.c.name)
        r = self.client.post("/payroll/transfers/toggle/", {"st": st.pk, "field": "paid", "on": "1"})
        self.assertTrue(r.json()["on"])
        self.client.post("/payroll/transfers/toggle/", {"st": st.pk, "field": "sent", "on": "1"})
        st.refresh_from_db()
        self.assertIsNotNone(st.delivery.paid_at)
        self.assertIsNotNone(st.delivery.sent_at)
        csv_text = self.client.get("/payroll/transfers/?month=2026-11&format=csv").content.decode("utf-8-sig")
        self.assertIn("済", csv_text)

    def test_confirm_blocked_by_unapproved_expense(self):
        from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile

        SalesCount.objects.create(member=self.a, month=NOV, kind="confirmed", count=0)
        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="u.png", sha256="u" * 64, file_format="image", document_kind="transport",
                                       target_month=NOV, status="parsed")
        ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=self.a, auto_category=cat, auto_amount=50000,
                                     auto_target_month=NOV, dedupe_key="u1")
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        page = self.client.get(f"/payroll/review/?month=2026-11&st={st.pk}")
        self.assertContains(page, "承認していない経費が 1件")
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "confirm"})
        st.refresh_from_db()
        self.assertNotEqual(st.status, "confirmed")


class RememberTodayTests(TestCase):
    def test_remember_today_skips_idle_timeout(self):
        from django.test import override_settings

        from apps.accounts import stepup as su

        su.set_password("payroll", "pay-pass")
        self.client.post("/stepup/payroll/", {"password": "pay-pass", "remember": "1", "next": "/payroll/"})
        with override_settings(STEPUP_IDLE_SECONDS=-1):
            self.assertEqual(self.client.get("/payroll/").status_code, 200)  # 今日は聞かない


class BackupReminderTests(TestCase):
    def test_home_shows_backup_notice(self):
        self.assertContains(self.client.get("/"), "バックアップがまだありません")


class ProductMemberConfirmedImportTests(TableScreenshotTests):
    def test_confirmed_sheet_goes_to_houmon_denki_for_product_member(self):
        from apps.payroll.models import PayrollItemDefinition, Product

        PayrollItemDefinition.objects.get(code="sales_commission").excluded_members.add(self.a)
        PayrollItemDefinition.objects.get(code="product_houmon_denki").members.add(self.a)
        text = "営業\t合計\n大石一郎\t119\n吉野健太\t30\n月合計\t149"
        self.client.post("/payroll/counts/?month=2026-11", {"step": "parse", "kind": "confirmed", "text": text})
        plan = self.client.session["counts_import"]
        data = {"step": "apply"}
        for r in plan["rows"]:
            data[f"count_{r['i']}"] = r["count"]
            data[f"member_{r['i']}"] = r["member_id"]
        self.client.post("/payroll/counts/?month=2026-11", data)
        houmon = Product.objects.get(code="houmon_denki")
        self.assertEqual(SalesCount.objects.get(member=self.a, month=NOV, kind="confirmed", product=houmon).count, 119)
        self.assertFalse(SalesCount.objects.filter(member=self.a, month=NOV, kind="confirmed", product__isnull=True).exists())
        self.assertEqual(SalesCount.objects.get(member=self.b, month=NOV, kind="confirmed", product__isnull=True).count, 30)


def _receipt_png(lines):
    import io

    from PIL import Image, ImageDraw, ImageFont

    f = ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 34)
    img = Image.new("RGB", (900, 80 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((60, 40 + 60 * i), ln, font=f, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class ReceiptAndLayoutTests(TableScreenshotTests):
    def test_receipts_for_chosen_member(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        from apps.expenses.models import ExpenseCategory, ExpenseRecord

        cat = ExpenseCategory.objects.exclude(code__in=["gasoline", "transport"]).first()
        png1 = _receipt_png(["サンプル商店", "2026年11月3日", "小計 1,364", "合計 ¥1,500"])  # 0 のない金額にはしないが、合計の行を読む
        png2 = _receipt_png(["架空ストア", "2026年11月5日", "合計 ¥2,365"])
        with override_settings(OCR_IN_BACKGROUND=False):
            self.client.post("/payroll/expenses/receipts/?month=2026-11", {
                "member": self.a.pk, "category": cat.pk,
                "files": [SimpleUploadedFile("r1.png", png1, content_type="image/png"),
                          SimpleUploadedFile("r2.png", png2, content_type="image/png")]})
        recs = list(ExpenseRecord.objects.filter(source_file__document_kind="other_expense").order_by("pk"))
        self.assertEqual(len(recs), 2)
        self.assertTrue(all(r.effective_member == self.a for r in recs))
        self.assertEqual(recs[1].effective_amount, 2365)
        self.assertTrue(all(r.status == "approved" for r in recs))  # 人を選んで入れたレシートはそのまま給与に入る
        # 給与の画面を開くと、計算し直して明細に入っている
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        self.client.get("/payroll/?month=2026-11")
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        line = st.lines.get(code=cat.payroll_item_code or "other_expense")
        self.assertEqual(line.amount, sum(r.effective_amount for r in recs))

    def test_other_expense_added_later_is_recalculated(self):
        from apps.expenses.models import ExpenseCategory

        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        before = st.net_amount
        cat = ExpenseCategory.objects.get(code="other_expense")
        self.client.post("/expenses/line/", {"text": f"{self.a.name} 1,234 駐車場", "category": cat.pk, "target_month": "2026-11"})
        self.client.get("/payroll/?month=2026-11")
        st.refresh_from_db()
        self.assertEqual(st.lines.get(code="other_expense").amount, 1234)
        self.assertEqual(st.net_amount, before + 1234)

    def test_team_bonus_only_for_leaders(self):
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        self.assertFalse(st.lines.filter(code="team_bonus").exists())  # リーダーでない人には項目を出さない


class RetiredAndUnitHintTests(TableScreenshotTests):
    def test_retired_member_with_counts_gets_statement_and_is_matched(self):
        from apps.members.matching import match_member

        old = Member.objects.create(name="退職 太郎", employment_type=C, status=Member.Status.RETIRED, retired_on=date(2026, 9, 30))
        SalesCount.objects.create(member=old, month=NOV, kind="confirmed", count=5)
        self.assertEqual(match_member("退職").member, old)
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        self.assertTrue(PayrollStatement.objects.filter(member=old, month=NOV).exists())
        page = self.client.get("/payroll/sales/?month=2026-11")
        self.assertContains(page, "退職 太郎")

    def test_unit_hint_on_counts_list(self):
        from apps.shifts.models import WorkType

        SalesCount.objects.create(member=self.a, month=OCT, kind="acquired", count=65)
        SalesCount.objects.create(member=self.a, month=NOV, kind="confirmed", count=40)
        from apps.payroll.models import PayrollStatement as _PS  # noqa: F401

        work_days(self.a, OCT, 20)
        page = self.client.get("/payroll/sales/?month=2026-11")
        self.assertContains(page, "単価")
        self.assertContains(page, "× 40件")


class ExpenseDeleteTests(TableScreenshotTests):
    def test_delete_mistaken_records(self):
        from apps.expenses.models import ExpenseCategory, ExpenseRecord, SourceFile

        cat = ExpenseCategory.objects.get(code="transport")
        sf = SourceFile.objects.create(original_name="m.png", sha256="m" * 64, file_format="image", document_kind="transport",
                                       target_month=NOV, status="parsed")
        a = ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=self.a, auto_category=cat, auto_amount=590,
                                         auto_target_month=NOV, dedupe_key="m1", status="approved")
        st_b = PayrollStatement.objects.create(member=self.b, month=NOV, employment_type=C)
        b = ExpenseRecord.objects.create(source_file=sf, row_number=2, auto_member=self.b, auto_category=cat, auto_amount=4499,
                                         auto_target_month=NOV, dedupe_key="m2", status="reflected", reflected_statement=st_b)
        page = self.client.get("/payroll/expenses/fix/?month=2026-11&group=transport")
        self.assertContains(page, f'name="delete_{a.pk}"')
        self.assertNotContains(page, f'name="delete_{b.pk}"')  # 確定済みの給与に入ったものは消せない
        self.client.post("/payroll/expenses/fix/", {"month": "2026-11", "group": "transport", "then": "",
                                                   f"member_{a.pk}": self.a.pk, f"amount_{a.pk}": "590", f"delete_{a.pk}": "1",
                                                   f"member_{b.pk}": self.b.pk, f"amount_{b.pk}": "4499"})
        self.assertFalse(ExpenseRecord.objects.filter(pk=a.pk).exists())
        self.assertTrue(ExpenseRecord.objects.filter(pk=b.pk).exists())
        self.assertTrue(SourceFile.objects.filter(pk=sf.pk).exists())  # まだ行が残っているので資料は残す


class StaffPayTests(TableScreenshotTests):
    """正社員：明細は作らず、給与＋交通費＋経費の合計を出して支出に入れる。"""

    def test_staff_total_goes_into_payments(self):
        from apps.expenses import importer
        from apps.expenses.models import ExpenseCategory
        from apps.members.models import Member
        from apps.payments.views import month_totals
        from apps.payroll.models import BaseSalary

        e = Member.objects.create(name="架空 社員", employment_type="employee", joined_on=date(2025, 1, 1))
        BaseSalary.objects.create(member=e, effective_from=date(2025, 1, 1), amount=250000)
        page = self.client.get("/payroll/staff/?month=2026-11")
        self.assertContains(page, "架空 社員")
        self.assertContains(page, "基本給")  # まだ保存していない時は基本給を使う
        before = month_totals(NOV)["payments"]
        self.client.post("/payroll/staff/?month=2026-11", {f"salary_{e.pk}": "260,000"})
        cat = ExpenseCategory.objects.get(code="transport")
        importer.import_line_text("架空社員 12,345", category=cat, target_month=NOV)
        from apps.expenses.models import ExpenseRecord
        importer.approve(list(ExpenseRecord.objects.filter(auto_member=e)), confirm_soft_issues=True)
        self.assertEqual(month_totals(NOV)["payments"] - before, 260000 + 12345 - 250000)  # 基本給→保存した額＋交通費
        from apps.payroll.staff import staff_rows
        row = next(r for r in staff_rows(NOV) if r["member"] == e)
        self.assertEqual((row["salary"], row["transport"], row["total"]), (260000, 12345, 272345))
        self.assertFalse(PayrollStatement.objects.filter(member=e).exists())  # 明細は作らない
        self.assertContains(self.client.get("/payments/?month=2026-11"), "正社員給与")


class TransportNoSubtractTests(TableScreenshotTests):
    def test_member_without_deduction_gets_full_transport(self):
        from apps.expenses import importer
        from apps.expenses.models import ExpenseCategory, ExpenseRecord

        cat = ExpenseCategory.objects.get(code="transport")
        importer.import_line_text(f"{self.a.name.replace(' ', '')} 50,000", category=cat, target_month=NOV)
        importer.approve(list(ExpenseRecord.objects.filter(auto_member=self.a)), confirm_soft_issues=True)
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        self.assertEqual(st.lines.get(code="transport_contractor").amount, 15000)
        self.client.post("/payroll/settings/transport/", {f"m_{self.a.pk}": "1"})
        self.assertEqual(st.lines.get(code="transport_contractor").amount, 50000)  # 引かない設定で全額


class PropaneDenkiTests(TableScreenshotTests):
    def test_propane_denki_same_unit_as_houmon_denki(self):
        from .models import PayrollItemDefinition, Product

        item = PayrollItemDefinition.objects.get(code="product_propane_denki")
        self.assertEqual(item.params, {"product": "propane_denki", "unit_price": "auto"})
        PayrollItemDefinition.objects.get(code="product_houmon_denki").members.add(self.a)
        item.members.add(self.a)
        for code, n in (("houmon_denki", 4), ("propane_denki", 3)):
            SalesCount.objects.create(member=self.a, month=NOV, kind="confirmed", product=Product.objects.get(code=code), count=n)
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        h, p = st.lines.get(code="product_houmon_denki"), st.lines.get(code="product_propane_denki")
        self.assertEqual(h.amount // 4, p.amount // 3)  # 単価は訪問電気と同じ


class UnitPriceOverrideTests(TableScreenshotTests):
    def test_manual_unit_price_keeps_days_and_counts(self):
        from .models import PayrollInput

        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        st = PayrollStatement.objects.get(member=self.a, month=NOV)
        days_before = st.inputs.get(key="prev_attendance_days").value
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "base", "unit_price": "7,777"})
        st.refresh_from_db()
        unit = st.inputs.get(key="unit_price")
        self.assertEqual(int(unit.value), 7777)
        self.assertEqual(st.inputs.get(key="prev_attendance_days").value, days_before)  # 出勤日数はそのまま
        line = st.lines.get(code="sales_commission")
        if line.quantity:
            self.assertEqual(line.unit_price, 7777)
        # 全員を計算し直しても手入力の単価は残る
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        self.assertEqual(int(PayrollInput.objects.get(statement=st, key="unit_price").value), 7777)
        # 空にすると自動に戻る
        self.client.post(f"/payroll/review/{st.pk}/", {"action": "base", "unit_price": ""})
        self.assertIsNone(PayrollInput.objects.get(statement=st, key="unit_price").override_value)
