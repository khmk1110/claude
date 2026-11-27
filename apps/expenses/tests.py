import io
import shutil
import tempfile
from datetime import date

from django.test import TestCase, override_settings

from apps.accounts import stepup
from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.expenses import importer, parsers
from apps.expenses.models import ExpenseCategory, ExpenseIssue, ExpenseRecord, SourceFile
from apps.members.models import CardCode, Member, MemberAlias
from apps.payroll import calc
from apps.payroll.models import PayrollStatement

R = ExpenseRecord.Status
TMP_MEDIA = tempfile.mkdtemp()


def transport_invoice_pdf(lines, issued="2026年10月5日") -> bytes:
    """請求書PDF（架空）を作る。品目の行に「交通費(8月分) 氏名 単価 数量 価格」を含む。"""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from apps.payroll.pdf import font_name

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    f = font_name()
    c.setFont(f, 11)
    y = 800
    c.drawString(40, y, f"請求書　発行日 {issued}")
    y -= 30
    c.drawString(40, y, "品目    単価    数量    単位    価格")
    for text in ["LPガス風呂有り 60,000 1 60,000", *lines, "システム費用(訪問) -3,300 3 -9,900"]:
        y -= 20
        c.drawString(40, y, text)
    c.save()
    return buf.getvalue()


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ImporterTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        from apps.payroll.tests import make_tables

        make_tables()
        self.user = User.objects.create_user("admin", password="x-pass-12345")
        self.c = Member.objects.create(member_no="C1", name="青木 一郎", employment_type="contractor")
        self.i = Member.objects.create(member_no="I1", name="井上 次郎", employment_type="intern")
        self.e = Member.objects.create(member_no="E1", name="社員 花子", employment_type="employee")
        self.n = Member.objects.create(member_no="I2", name="渡邊 三郎", employment_type="intern")

    def test_card_code_uses_bracketed_4_digits_not_last_digit(self):
        self.assertEqual(parsers.card_code_from("0566-007-4 S [ R0007 ]")[0], "0007")
        self.assertEqual(parsers.card_code_from("1234-5678-9012-3456〔0003〕"), ("0003", "3456"))
        self.assertEqual(parsers.card_code_from("RO010")[0], "0010")  # OCR の O→0

    def test_transport_invoice_pdf(self):
        pdf = transport_invoice_pdf([
            "交通費(8月分) 青木 98,000 1 98,000",
            "交通費(8月分) 井上 5,000 1 5,000",
            "交通費(8月分) 渡辺 6,400 1 6,400",
            "交通費(8月分) 社員 40,000 1 40,000",
        ])
        sf = importer.import_file(pdf, "invoice.pdf", user=self.user)
        self.assertEqual((sf.parser, sf.document_kind), ("transport_invoice", "transport"))
        self.assertEqual(sf.document_month, date(2026, 8, 1))
        self.assertEqual(sf.target_month, date(2026, 10, 1))  # 8月分 → 10月の給与
        recs = {r.name_text: r for r in sf.records.all()}
        self.assertEqual(set(recs), {"青木", "井上", "渡辺", "社員"})  # LPガスなど交通費以外の行は読まない
        self.assertEqual((recs["青木"].auto_member, recs["青木"].auto_amount, recs["青木"].status), (self.c, 98000, R.UNCHECKED))
        self.assertEqual(recs["社員"].status, R.UNCHECKED)  # 正社員の交通費は「正社員の給与」の合計に入る（明細は作らない）
        watanabe = recs["渡辺"]  # 「邊」と「辺」は同じ字として照合（承認するまで給与には入らない）
        self.assertEqual((watanabe.auto_member, watanabe.status), (self.n, R.UNCHECKED))
        ok, skipped = importer.approve(list(sf.records.all()), user=self.user)
        self.assertEqual(ok, 4)
        # 同じファイルは二重に取り込まない
        with self.assertRaises(importer.DuplicateFile):
            importer.import_file(pdf, "invoice_copy.pdf", user=self.user)
        # 給与計算に反映（個人事業主は −35,000円、インターンはそのまま）。−35,000円は契約の報酬がある月だけ
        from apps.payroll.models import SalesCount
        SalesCount.objects.create(member=self.c, month=date(2026, 10, 1), kind="confirmed", count=3)
        calc.prepare_month(date(2026, 10, 1))
        st = PayrollStatement.objects.get(member=self.c, month=date(2026, 10, 1))
        st = calc.calculate(st)
        self.assertEqual(st.lines.get(code="transport_contractor").amount, 98000 - 35000)
        sti = calc.calculate(PayrollStatement.objects.get(member=self.n, month=date(2026, 10, 1)))
        self.assertEqual(sti.lines.get(code="transport_intern").amount, 6400)

    def test_csv_mapping_saved_and_reused_and_header_change_asks_again(self):
        csv1 = "利用日,担当,区間,金額\n2026/09/03,青木 一郎,渋谷-新宿,1200\n2026/09/10,井上,新宿-池袋,800\n合計,,,2000\n".encode("cp932")
        sf = importer.import_file(csv1, "交通費9月.csv", user=self.user)
        self.assertEqual(sf.status, SourceFile.Status.NEEDS_MAPPING)
        self.assertEqual(sf.headers, ["利用日", "担当", "区間", "金額"])
        importer.save_mapping(sf, user=self.user, name="交通費CSV", kind="transport",
                              column_map={"used_on": "利用日", "name": "担当", "route": "区間", "amount": "金額"},
                              category=ExpenseCategory.objects.get(code="transport"), payroll_month_offset=1)
        sf.refresh_from_db()
        self.assertEqual((sf.status, sf.row_count, sf.source_total, sf.target_month), ("parsed", 2, 2000, date(2026, 10, 1)))
        self.assertFalse(sf.issues.filter(code="total_mismatch").exists())
        # 同じ見出しの別ファイルは自動で読み取る
        csv2 = "利用日,担当,区間,金額\n2026/10/01,青木 一郎,渋谷-品川,500\n".encode("utf-8")
        sf2 = importer.import_file(csv2, "交通費10月.csv", user=self.user)
        self.assertEqual((sf2.status, sf2.row_count), ("parsed", 1))
        # 見出しが変わったら勝手に読み替えない
        csv3 = "日付,担当者,区間,金額\n2026/10/01,青木 一郎,渋谷-品川,500\n".encode("utf-8")
        self.assertEqual(importer.import_file(csv3, "交通費_新形式.csv").status, SourceFile.Status.NEEDS_MAPPING)

    def test_card_assignment_unknown_card_ignored_and_total_mismatch(self):
        CardCode.objects.create(code="0007", member=self.c)
        CardCode.objects.create(code="0001", ignored=True, label="正社員")
        csv = "カード番号,利用日,金額\n0566-007-4 S [ R0007 ],2026/09/02,80000\n0566-001-7 S [ R0001 ],2026/09/02,30000\n" \
              "0566-009-9 S [ R0009 ],2026/09/03,7000\n合計,,130000\n"
        sf = importer.import_file(csv.encode(), "gas.csv", kind="gasoline")
        importer.save_mapping(sf, name="ガソリンCSV", kind="gasoline", column_map={"card": "カード番号", "used_on": "利用日", "amount": "金額"},
                              category=ExpenseCategory.objects.get(code="gasoline"), payroll_month_offset=1)
        recs = {r.card_code: r for r in sf.records.all()}
        self.assertEqual((recs["0007"].auto_member, recs["0007"].status), (self.c, R.UNCHECKED))
        self.assertEqual(recs["0001"].status, R.EXCLUDED)
        self.assertEqual(recs["0009"].status, R.EXCLUDED)  # 対応表にないカードは無視する
        self.assertIn("対応表にない", recs["0009"].note)
        self.assertTrue(sf.issues.filter(code="total_mismatch", resolved=False).exists())  # 110,000 ≠ 130,000

    def test_duplicate_transaction_is_flagged_not_deleted(self):
        CardCode.objects.create(code="0007", member=self.c)
        rows = "カード,日付,金額,取引番号\n[0007],2026/09/02,1000,T-1\n"
        a = importer.import_file(rows.encode(), "a.csv", kind="gasoline")
        importer.save_mapping(a, name="g", kind="gasoline", column_map={"card": "カード", "used_on": "日付", "amount": "金額", "txn_id": "取引番号"})
        b = importer.import_file((rows + "\n").encode(), "b.csv", kind="gasoline")  # 中身が同じ取引の別ファイル
        rec = b.records.get()
        self.assertEqual(rec.status, R.PENDING)
        self.assertTrue(rec.issues.filter(code="duplicate_txn").exists())
        self.assertEqual(ExpenseRecord.objects.count(), 2)  # 自動削除しない

    def test_reflected_record_locked_and_change_after_confirm_flags_payroll(self):
        sf = SourceFile.objects.create(original_name="m", sha256="m" * 64, file_format="csv", target_month=date(2026, 10, 1))
        cat = ExpenseCategory.objects.get(code="advance")
        rec = importer.add_manual_record(sf, member=self.c, category=cat, amount=5000, target_month=date(2026, 10, 1), reason="立替")
        importer.approve([rec])
        calc.prepare_month(date(2026, 10, 1))
        st = PayrollStatement.objects.get(member=self.c, month=date(2026, 10, 1))
        calc.set_input_override(st, "prev_attendance_days", 20, reason="r")
        calc.set_input_override(st, "prev_acquired_count", 10, reason="r")
        st = calc.set_input_override(st, "confirmed_count", 1, reason="r")
        self.assertEqual(st.lines.get(code="advance").amount, 5000, st.calc_messages)
        self.assertEqual(st.status, "calculated", st.calc_messages)
        calc.confirm(st)
        rec.refresh_from_db()
        self.assertEqual(rec.status, R.REFLECTED)
        with self.assertRaises(importer.RecordLocked):
            importer.update_record(rec, reason="x", amount=1)
        # 確定後に経費が追加されたら、給与に「要確認」を立てる（金額は変えない）
        rec2 = importer.add_manual_record(sf, member=self.c, category=cat, amount=3000, target_month=date(2026, 10, 1), reason="追加")
        importer.approve([rec2])
        st.refresh_from_db()
        self.assertTrue(st.needs_review)
        self.assertEqual(st.snapshot["totals"]["net"], st.net_amount)
        self.assertTrue(rec2.issues.filter(code="changed_after_payroll").exists())

    def test_edit_requires_reason_and_keeps_auto_value(self):
        sf = SourceFile.objects.create(original_name="m", sha256="n" * 64, file_format="csv", target_month=date(2026, 10, 1))
        rec = ExpenseRecord.objects.create(source_file=sf, row_number=1, auto_member=self.c, auto_amount=1000,
                                           auto_category=ExpenseCategory.objects.get(code="transport"), dedupe_key="k")
        with self.assertRaises(ValueError):
            importer.update_record(rec, reason="", amount=900)
        importer.update_record(rec, reason="領収書と照合", amount=900, user=self.user)
        rec.refresh_from_db()
        self.assertEqual((rec.auto_amount, rec.amount, rec.effective_amount), (1000, 900, 900))
        log = AuditLog.objects.get(action="expenses.record_update")
        self.assertEqual((log.before["金額"], log.after["金額"], log.reason), (1000, 900, "領収書と照合"))


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ExpenseViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("staff", password="x-pass-12345")
        stepup.set_password("expenses", "exp-pass")
        stepup.set_password("payroll", "pay-pass")
        Member.objects.create(member_no="C1", name="青木 一郎", employment_type="contractor")
        self.client.force_login(self.user)

    def test_gate_and_pages(self):
        for url in ["/expenses/", "/expenses/files/1/", "/expenses/files/1/download/"]:
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        self.assertEqual(self.client.get("/expenses/").status_code, 200)  # 経費は給与のパスワードで開く
        pdf = transport_invoice_pdf(["交通費(8月分) 青木 10,000 1 10,000"])
        from django.core.files.uploadedfile import SimpleUploadedFile

        res = self.client.post("/expenses/upload/", {"files": [SimpleUploadedFile("t.pdf", pdf, "application/pdf")]})
        sf = SourceFile.objects.get()
        self.assertRedirects(res, f"/expenses/files/{sf.pk}/", fetch_redirect_response=False)
        page = self.client.get(f"/expenses/files/{sf.pk}/")
        self.assertContains(page, "青木 一郎")
        self.assertEqual(self.client.get(f"/expenses/files/{sf.pk}/download/").status_code, 200)
        rec = sf.records.get()
        self.client.post(f"/expenses/files/{sf.pk}/action/", {"action": "approve", "records": [rec.pk]})
        rec.refresh_from_db()
        self.assertEqual(rec.status, R.APPROVED)
        self.assertContains(self.client.get("/expenses/?month=2026-10"), "10,000")


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class LineAndOcrConfirmTests(TestCase):
    def setUp(self):
        self.a = Member.objects.create(member_no="I1", name="関口 一郎", employment_type="intern")
        self.b = Member.objects.create(member_no="I2", name="甲斐 二郎", employment_type="intern")

    def test_line_text_import(self):
        sf = importer.import_line_text("関口 600\n甲斐 5,950円 追加分\nよくわからない行", category=ExpenseCategory.objects.get(code="transport"),
                                       target_month=date(2026, 10, 1))
        recs = {r.name_text: r for r in sf.records.all()}
        self.assertEqual((recs["関口"].auto_member, recs["関口"].auto_amount), (self.a, 600))
        self.assertEqual((recs["甲斐"].auto_member, recs["甲斐"].auto_amount, recs["甲斐"].auto_description), (self.b, 5950, "追加分"))
        self.assertTrue(sf.issues.filter(message__contains="よくわからない行").exists())
        self.assertEqual(importer.approve(list(sf.records.all()))[0], 2)

    def test_confirm_amounts_resolves_low_confidence(self):
        CardCode.objects.create(code="0003", member=self.a)
        sf = SourceFile.objects.create(original_name="ocr.pdf", sha256="o" * 64, file_format="pdf", target_month=date(2026, 10, 1),
                                       used_ocr=True)
        rec = ExpenseRecord(source_file=sf, row_number=1, card_code="0003", auto_amount=None, confidence=0.3,
                            auto_category=ExpenseCategory.objects.get(code="gasoline"), auto_target_month=date(2026, 10, 1))
        rec.dedupe_key = "x"
        rec.save()
        importer.evaluate(rec, initial=True)
        self.assertEqual(rec.status, R.PENDING)
        importer.confirm_amounts(sf, {rec.pk: 28000})
        self.assertFalse(rec.issues.filter(resolved=False).exists())
        self.assertEqual(importer.approve([rec])[0], 1)  # 古いインスタンスを渡しても最新の金額で承認する（上書きしない）
        rec.refresh_from_db()
        self.assertEqual((rec.effective_amount, rec.status), (28000, R.APPROVED))


class TransportOcrSpacingTests(TestCase):
    def test_spaced_ocr_lines_are_read(self):
        from datetime import date as _d

        from apps.expenses.parsers import parse_transport_invoice

        text = "交通 費 (8 月 分 ) 大 石 52,080 1 52,080\n交通 費 (8 月 分 ) 長谷 川 花子 6,822 1 6,822"
        doc = parse_transport_invoice([text], _d(2026, 11, 1))
        self.assertEqual([(r.name, r.amount) for r in doc.rows], [("大石", 52080), ("長谷川花子", 6822)])


class TransportFormatVariantsTests(TestCase):
    def test_variants(self):
        from datetime import date as _d

        from apps.expenses.parsers import parse_transport_invoice

        text = ("交通費 10月分 大石 ¥52,080 1 式 ¥52,080\n"
                "交通費（10月分） 長谷川 6,822円 1 6,822円\n"
                "交通費(10月分) 吉野 7,184\n"
                "システム費用(訪問) -3,300 3 -9,900")
        doc = parse_transport_invoice([text], _d(2026, 11, 1))
        self.assertEqual([(r.name, r.amount) for r in doc.rows], [("大石", 52080), ("長谷川", 6822), ("吉野", 7184)])
        self.assertEqual(doc.document_month, _d(2026, 10, 1))


@override_settings(OCR_IN_BACKGROUND=False)
class TransportReimportTests(TestCase):
    def _png(self):
        import io

        from PIL import Image, ImageDraw, ImageFont

        f = ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 30)
        img = Image.new("RGB", (1206, 500), "white")
        d = ImageDraw.Draw(img)
        d.text((40, 40), "品目                     単価     数量    価格", font=f, fill="black")
        for i, (n, a) in enumerate([("大石", "52,080"), ("長谷川", "6,822")]):
            y = 110 + 60 * i
            d.text((40, y), f"交通費(8月分)  {n}", font=f, fill="black")
            d.text((560, y), a, font=f, fill="black")
            d.text((800, y), "1", font=f, fill="black")
            d.text((1000, y), a, font=f, fill="black")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def test_same_file_as_gasoline_then_transport(self):
        from datetime import date as _d

        from apps.expenses import importer

        data = self._png()
        sf = importer.import_file(data, "inv.png", kind="gasoline", target_month=_d(2026, 11, 1))
        self.assertEqual(sf.document_kind, "transport")  # ガソリン代として読めないので交通費として取り込む
        self.assertEqual(sorted(r.effective_amount for r in sf.records.all()), [6822, 52080])
        sf.records.all().delete()
        sf.row_count = 0
        sf.save()
        again = importer.import_file(data, "inv.png", kind="transport", target_month=_d(2026, 11, 1))  # 重複でも読み直す
        self.assertEqual(again.pk, sf.pk)
        self.assertEqual(again.records.count(), 2)


@override_settings(OCR_IN_BACKGROUND=True)
class StuckProcessingRecoveryTests(TestCase):
    """サーバーの再起動などで裏の読み取りが消えても、「読み取り中」のまま止まらない。"""

    def _sf(self, minutes_ago):
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        sf = SourceFile.objects.create(original_name="t.jpg", sha256=f"x{minutes_ago}", size=1, file_format="image",
                                       document_kind="transport", status="processing",
                                       processing_started_at=timezone.now() - timedelta(minutes=minutes_ago))
        return sf, mock

    def test_lost_job_is_requeued(self):
        sf, mock = self._sf(1)
        with mock.patch.object(importer, "_start_background") as start:
            importer.recover_processing()
        start.assert_called_once_with(sf.pk)

    def test_queued_job_is_left_alone(self):
        from apps.core import jobs

        sf, mock = self._sf(1)
        with mock.patch.object(jobs, "is_queued", return_value=True), mock.patch.object(importer, "_start_background") as start:
            importer.recover_processing()
        start.assert_not_called()

    def test_too_old_becomes_error_with_retry(self):
        sf, _ = self._sf(30)
        importer.recover_processing()
        sf.refresh_from_db()
        self.assertEqual(sf.status, "error")
        self.assertIn("もう一度読み取る", sf.error_message)

    def test_jobs_run_one_at_a_time_and_dedupe(self):
        import threading

        from apps.core import jobs

        done = threading.Event()
        gate = threading.Event()
        self.assertTrue(jobs.submit(("t", 1), lambda: (gate.wait(5), done.set())))
        self.assertFalse(jobs.submit(("t", 1), lambda: None))
        self.assertTrue(jobs.is_queued(("t", 1)))
        gate.set()
        self.assertTrue(done.wait(5))
