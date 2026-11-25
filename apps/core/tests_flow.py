"""STEP 7: 毎月の業務フローを最初から最後まで通す結合テスト（架空データ）。

シフト作成 → 勤怠予定 → 勤怠確定 → 資料取り込み（交通費PDF・ガソリンCSV・LINEの追加分）
→ 振り分け・承認 → 営業件数 → 単価判定 → 給与計算 → 確定 → 明細PDF → 確定後の変更通知
"""

import shutil
import tempfile
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts import stepup
from apps.accounts.models import User
from apps.attendance import services as attendance
from apps.core.models import AuditLog
from apps.expenses import importer
from apps.expenses.models import ExpenseCategory, ExpenseRecord
from apps.expenses.tests import transport_invoice_pdf
from apps.members.models import CardCode, Member
from apps.payroll.models import PayrollStatement, SalesCount, Team, TeamMembership
from apps.payroll.tests import make_tables
from apps.shifts import services as shifts
from apps.shifts.models import WorkType

TMP = tempfile.mkdtemp()
SEP, OCT, NOV = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)


@override_settings(MEDIA_ROOT=TMP)
class MonthlyFlowTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        make_tables()
        self.user = User.objects.create_user("kanri", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        stepup.set_password("expenses", "exp-pass")
        self.client.force_login(self.user)
        self.a = Member.objects.create(member_no="C1", name="青木 一郎", employment_type="contractor", display_order=1)
        self.b = Member.objects.create(member_no="I1", name="井上 次郎", employment_type="intern", display_order=2)
        self.c = Member.objects.create(member_no="I2", name="上野 三郎", employment_type="intern", display_order=3)
        self.e = Member.objects.create(member_no="E1", name="江口 花子", employment_type="employee", display_order=4)
        CardCode.objects.create(code="0007", member=self.a)
        CardCode.objects.create(code="0001", ignored=True, label="正社員")
        team = Team.objects.create(name="インターン", leader=self.b, kind=Team.Kind.INTERN)
        TeamMembership.objects.create(team=team, member=self.c, start_month=SEP)

    def test_full_month(self):
        work, half = WorkType.objects.get(code="work"), WorkType.objects.get(code="half")
        # 1〜3. 10月のシフト → 勤怠予定 → 勤怠修正・月次確定
        shifts.set_shifts([self.a], [OCT.replace(day=d) for d in range(1, 21)], work, user=self.user)  # 20日
        shifts.set_shifts([self.a], [OCT.replace(day=21)], half, user=self.user)
        shifts.set_shifts([self.b], [OCT.replace(day=d) for d in range(1, 9)], work, user=self.user)
        attendance.update_actual(self.a, OCT.replace(day=20), work_type=half, user=self.user, reason="午後休み")
        attendance.lock_month(OCT, user=self.user)
        shifts.set_shifts([self.b], [NOV.replace(day=d) for d in range(1, 6)], work, user=self.user)  # 当月5日（手当の条件）

        # 4〜7. 資料の取り込み・振り分け・承認（画面から）
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})  # 経費は給与のパスワードで開く
        pdf = transport_invoice_pdf(["交通費(9月分) 青木 50,000 1 50,000", "交通費(9月分) 井上 3,000 1 3,000",
                                     "交通費(9月分) 江口 20,000 1 20,000"], issued="2026年10月5日")
        self.client.post("/expenses/upload/", {"files": [SimpleUploadedFile("transport.pdf", pdf)]})
        gas = "カード,利用日,金額\n[0007],2026/10/03,8000\n[0007],2026/10/20,4500\n[0001],2026/10/05,9999\n".encode()
        self.client.post("/expenses/upload/", {"files": [SimpleUploadedFile("gas.csv", gas)], "kind": "gasoline"})
        gas_file = importer.SourceFile.objects.get(original_name="gas.csv")
        self.client.post(f"/expenses/files/{gas_file.pk}/mapping/", {
            "name": "ガソリンCSV", "kind": "gasoline", "category": ExpenseCategory.objects.get(code="gasoline").pk,
            "col_card": "カード", "col_used_on": "利用日", "col_amount": "金額", "offset": "1"})
        self.client.post("/expenses/line/", {"text": "井上 600", "category": ExpenseCategory.objects.get(code="transport").pk,
                                             "target_month": "2026-11", "note": "交通費の追加分"})
        approvable = ExpenseRecord.objects.exclude(status=ExpenseRecord.Status.EXCLUDED)
        self.assertEqual(approvable.count(), 6)  # 交通費3（正社員を含む）・ガソリン2・LINE1（対象外カードは除外）
        for sf in importer.SourceFile.objects.all():
            ids = list(sf.records.exclude(status="excluded").values_list("pk", flat=True))
            self.client.post(f"/expenses/files/{sf.pk}/action/", {"action": "approve", "records": ids})
        self.assertEqual(ExpenseRecord.objects.filter(status="approved").count(), 6)  # 正社員の交通費も承認（明細には入らない）

        # 9〜10. 営業件数（10月の獲得 → 11月の単価、11月の入金確定 → 報酬）
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        # 11月分の給与の「件数を直す」画面：前月（10月）の獲得件数と、11月の入金確定件数を1つの表で入れる
        self.client.post("/payroll/sales/", {"month": "2026-11", f"acq_{self.a.pk}": "65", f"acq_{self.b.pk}": "30", f"acq_{self.c.pk}": "",
                                             f"conf_{self.a.pk}": "40", f"conf_{self.b.pk}": "25", f"conf_{self.c.pk}": "12"})
        # 11. 給与計算
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "prepare"})
        self.client.post("/payroll/action/", {"month": "2026-11", "action": "calculate_all"})
        a = PayrollStatement.objects.get(member=self.a, month=NOV)
        b = PayrollStatement.objects.get(member=self.b, month=NOV)
        self.assertFalse(PayrollStatement.objects.filter(member=self.e).exists())  # 正社員は明細なし
        ia = {i.key: i.value for i in a.inputs.all()}
        self.assertEqual(ia["prev_attendance_days"], 20)  # 19日＋半日2回 = 20.0（勤怠の修正を反映）
        self.assertEqual(ia["prev_acquired_count"], 65)
        unit_a = 1000 * 4 + 10 * 3  # 19〜21日・60〜79件（架空単価）
        la = {l.code: l.amount for l in a.lines.all()}
        self.assertEqual(la["sales_commission"], 40 * unit_a)
        self.assertEqual(la["transport_contractor"], 50000 - 35000)
        self.assertEqual(la["gasoline"], 8000 + 4500)
        self.assertEqual(la["app_fee"], 4000)
        self.assertEqual(a.net_amount, 40 * unit_a + 15000 - 12500 - 4000)
        lb = {l.code: l.amount for l in b.lines.all()}
        self.assertEqual(lb["transport_intern"], 3000 + 600)
        self.assertEqual(lb["intern_leader"], 12 * 500)  # 当月5日出勤 ≥ 4日、メンバー12件
        unit_b = 1000 * 3 + 10 * 2  # 8〜10日・30〜39件
        self.assertEqual(lb["sales_commission"], 25 * unit_b)

        # 12〜13. 確認・確定（確認チェックをしないと確定できない）
        self.client.post(f"/payroll/{a.pk}/action/", {"action": "confirm"})
        a.refresh_from_db()
        self.assertFalse(a.is_confirmed)
        for st in (a, b):
            self.client.post(f"/payroll/review/{st.pk}/", {"action": "checks", "chk_leader": "1", "chk_gas": "1", "chk_deduct": "1"})
        for st in (a, b):
            self.client.post(f"/payroll/{st.pk}/action/", {"action": "confirm"})
        a.refresh_from_db()
        self.assertTrue(a.is_confirmed)
        self.assertEqual(ExpenseRecord.objects.filter(status="reflected").count(), 5)  # 二重反映しない
        # 14. 明細PDF（個人別・一括）
        self.assertTrue(self.client.get(f"/payroll/{a.pk}/pdf/").content.startswith(b"%PDF"))
        self.assertTrue(self.client.get("/payroll/pdf/?month=2026-11").content.startswith(b"PK"))  # 1人1つのPDFをZIPで
        # 確定後に経費が追加されても金額は変わらず、要確認になる
        net = a.net_amount
        self.client.post("/expenses/line/", {"text": "青木 1,000", "category": ExpenseCategory.objects.get(code="advance").pk,
                                             "target_month": "2026-11"})
        rec = ExpenseRecord.objects.latest("pk")
        self.assertEqual(rec.status, "approved")  # 自分で入れた立替金・その他の経費はそのまま承認される
        a.refresh_from_db()
        self.assertEqual((a.net_amount, a.needs_review), (net, True))
        # 主要な操作がすべて監査ログに残っている
        actions = set(AuditLog.objects.values_list("action", flat=True))
        for act in ("shift.update", "attendance.update", "attendance.lock_month", "expenses.import", "expenses.approve",
                    "payroll.sales_count", "payroll.calculate", "payroll.confirm", "payroll.pdf", "payroll.pdf_bulk",
                    "stepup.success", "payroll.flag_review"):
            self.assertIn(act, actions)
