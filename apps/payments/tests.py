import io
from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts import stepup

from . import invoice
from .models import Payee, Payment, Sale

NOV = date(2026, 11, 1)

INVOICE_TEXT = """請求書
架空商事株式会社 御中
2026年11月5日
株式会社サンプル物流
東京都架空区1-2-3 TEL 03-0000-0000
ご請求金額 ¥108,251-
品目 数量 単価 金額
配送料 1 98,410 98,410
消費税 9,841
合計 108,251
お支払期限 2026年11月30日
お振込先 みずほ銀行 渋谷支店 普通 1234567
口座名義 カ)サンプルブツリュウ"""


def invoice_png(lines):
    from PIL import Image, ImageDraw, ImageFont

    f = ImageFont.truetype("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", 30)
    img = Image.new("RGB", (1200, 80 + 56 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((50, 40 + 56 * i), ln, font=f, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class InvoiceGuessTests(TestCase):
    def test_fields_from_text(self):
        g = invoice.guess(INVOICE_TEXT, own_company="架空商事株式会社", today=date(2026, 11, 10))
        self.assertEqual(g.amount, 108251)
        self.assertEqual(g.due_date, date(2026, 11, 30))
        self.assertIn("株式会社サンプル物流", g.payee)
        self.assertIn("みずほ銀行", g.bank)

    def test_known_payee_preferred(self):
        g = invoice.guess(INVOICE_TEXT, own_company="架空商事株式会社", known_payees=["サンプル物流"])
        self.assertEqual(g.payee, "サンプル物流")


@override_settings(OCR_IN_BACKGROUND=False)
class PaymentFlowTests(TestCase):
    def setUp(self):
        stepup.set_password("payroll", "pay-pass")

    def unlock(self):
        self.client.post("/stepup/payroll/", {"password": "pay-pass", "next": "/payments/"})

    def test_needs_payroll_password(self):
        self.assertIn("/stepup/payroll/", self.client.get("/payments/")["Location"])

    def test_upload_confirm_pay_and_remember_payee(self):
        self.unlock()
        # 画像のテスト用の字体は 0 に点があり 9 と読まれやすいので、0 のない金額にする
        png = invoice_png(["株式会社サンプル物流", "ご請求金額 125,451円", "お支払期限 2026年11月30日", "お振込先 みずほ銀行 渋谷支店 普通 1234567"])
        self.client.post("/payments/upload/?month=2026-11", {"files": [SimpleUploadedFile("inv.png", png, content_type="image/png")]})
        p = Payment.objects.get()
        self.assertEqual(p.status, "draft")
        self.assertEqual(p.amount, 125451)
        self.assertIn("サンプル物流", p.payee_name)
        self.assertContains(self.client.get("/payments/?month=2026-11"), "確認待ち")
        # 確認して保存（分類を付ける・毎月払う）
        self.client.post(f"/payments/{p.pk}/", {"payee_name": "サンプル物流", "amount": "125,451", "category": "外注・業務委託",
                                                "bank": p.bank, "due_date": "2026-11-30", "month": "2026-11", "monthly": "1", "monthly_shown": "1"})
        p.refresh_from_db()
        self.assertEqual(p.status, "unpaid")
        payee = Payee.objects.get(name="サンプル物流")
        self.assertTrue(payee.monthly)
        self.assertIn("みずほ銀行", payee.bank)
        # 支払った
        r = self.client.post(f"/payments/{p.pk}/paid/", {"on": "1"})
        self.assertTrue(r.json()["on"])
        p.refresh_from_db()
        self.assertEqual(p.status, "paid")
        # 来月：毎月の支払いを入れる
        self.client.post("/payments/monthly/?month=2026-12")
        dec = Payment.objects.get(month=date(2026, 12, 1))
        self.assertEqual((dec.payee_name, dec.amount, dec.category), ("サンプル物流", 125451, "外注・業務委託"))
        csv_text = self.client.get("/payments/?month=2026-11&format=csv").content.decode("utf-8-sig")
        self.assertIn("支払い済み", csv_text)

    def test_import_old_sheet(self):
        import openpyxl

        self.unlock()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "10月"
        ws["A1"] = "2026年10月 収支表"
        ws["D8"], ws["E8"], ws["F8"] = "支払相手", "支払い金額（円）", "状況"
        ws["D9"], ws["E9"], ws["F9"] = "架空運送", 108251, "支払い済"
        ws["D10"], ws["E10"] = "事務所家賃", 30000
        buf = io.BytesIO()
        wb.save(buf)
        f = SimpleUploadedFile("sheet.xlsx", buf.getvalue())
        self.client.post("/payments/import-sheet/?month=2026-11", {"file": f})
        oct_ = {p.payee_name: p.status for p in Payment.objects.filter(month=date(2026, 10, 1))}
        self.assertEqual(oct_, {"架空運送": "paid", "事務所家賃": "unpaid"})
        f2 = SimpleUploadedFile("sheet.xlsx", buf.getvalue())
        self.client.post("/payments/import-sheet/?month=2026-11", {"file": f2})  # 二重に入れない
        self.assertEqual(Payment.objects.count(), 2)


@override_settings(OCR_IN_BACKGROUND=False)
class SalesAndPayrollRowTests(TestCase):
    def setUp(self):
        stepup.set_password("payroll", "pay-pass")
        self.client.post("/stepup/payroll/", {"password": "pay-pass", "next": "/payments/"})

    def test_payroll_confirmed_total_is_added_automatically(self):
        from apps.members.models import Member
        from apps.payroll.models import PayrollStatement, StatementDelivery

        m = Member.objects.create(name="大石 一郎", employment_type="contractor")
        st = PayrollStatement.objects.create(member=m, month=NOV, employment_type="contractor")
        PayrollStatement.objects.filter(pk=st.pk).update(status="confirmed", net_amount=250000, snapshot={"lines": []})
        Payment.objects.create(month=NOV, payee_name="事務所家賃", amount=30000, status="unpaid")
        page = self.client.get("/payments/?month=2026-11")
        self.assertContains(page, "個人事業主・インターン給与")
        self.assertContains(page, "280,000")  # 支払い合計に給与の確定額が入る
        StatementDelivery.objects.create(statement_id=st.pk, paid_at="2026-11-30T10:00:00+09:00")
        self.assertContains(self.client.get("/payments/?month=2026-11"), "全員振込済み")

    def test_sales_manual_entry_and_yearly(self):
        self.client.post("/payments/sales/new/?month=2026-11", {"client": "架空商事", "amount": "500,000", "month": "2026-11"})
        x = Sale.objects.get()
        self.assertFalse(x.received)
        self.client.post(f"/payments/sales/{x.pk}/received/", {"on": "1"})
        x.refresh_from_db()
        self.assertTrue(x.received)
        self.client.post("/payments/new/?month=2026-11", {"payee_name": "事務所家賃", "amount": "120000", "month": "2026-11"})
        page = self.client.get("/payments/year/?month=2026-11")
        self.assertContains(page, "500,000")
        self.assertContains(page, "380,000")  # 500,000 − 120,000

    def test_sheet_import_includes_sales(self):
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "2026年10月 収支表"
        ws["A9"], ws["B9"], ws["C9"] = "架空商事", 668736, "入金済"
        ws["D9"], ws["E9"] = "架空運送", 1000
        buf = io.BytesIO()
        wb.save(buf)
        self.client.post("/payments/import-sheet/?month=2026-11", {"file": SimpleUploadedFile("s.xlsx", buf.getvalue())})
        sale = Sale.objects.get()
        self.assertEqual((sale.client, sale.amount, sale.received), ("架空商事", 668736, True))


class SaleDocumentTests(TestCase):
    def test_guess_client_from_our_invoice_and_from_payment_notice(self):
        own = "架空商事株式会社"
        ours = "請求書\n株式会社サンプル電力 御中\n架空商事株式会社\nご請求金額 668,736円\nお支払期限 2026年12月10日"
        g = invoice.guess_sale(ours, own_company=own, today=date(2026, 11, 20))
        self.assertEqual((g.payee, g.amount, g.due_date), ("株式会社サンプル電力", 668736, date(2026, 12, 10)))
        notice = "支払通知書\n架空商事株式会社 御中\n株式会社サンプルガス\nお支払金額 4,819,000円"
        g = invoice.guess_sale(notice, own_company=own)
        self.assertEqual((g.payee, g.amount), ("株式会社サンプルガス", 4819000))

    @override_settings(OCR_IN_BACKGROUND=False)
    def test_upload_confirm_and_totals(self):
        stepup.set_password("payroll", "pay-pass")
        self.client.post("/stepup/payroll/", {"password": "pay-pass", "next": "/payments/"})
        png = invoice_png(["請求書", "株式会社サンプル電力 御中", "ご請求金額 668,736円"])  # 0 のない金額
        self.client.post("/payments/sales/upload/?month=2026-11", {"files": [SimpleUploadedFile("s.png", png, content_type="image/png")]})
        x = Sale.objects.get()
        self.assertEqual(x.state, "draft")
        self.assertEqual(x.amount, 668736)
        self.assertIn("サンプル電力", x.client)
        self.assertContains(self.client.get("/payments/sales/?month=2026-11"), "確認待ち")
        self.assertNotContains(self.client.get("/payments/year/?month=2026-11"), "668,736")  # 確認前は合計に入れない
        self.client.post(f"/payments/sales/{x.pk}/", {"client": x.client, "amount": "668736", "month": "2026-11"})
        x.refresh_from_db()
        self.assertEqual(x.state, "ok")
        self.assertContains(self.client.get("/payments/year/?month=2026-11"), "668,736")


class InvoiceVariantsTests(TestCase):
    """よくある請求書の書き方（架空の会社）。"""

    def test_column_header_amount_and_phone_not_amount(self):
        text = ("請求書\n架空商事株式会社 御中\nサンプル会計事務所\n350-0046\n架空県架空市1-2-3\n電話:080 5426 8557\n"
                "小計 消費税 源泉所得税 請求金額\n20000 円 2000.000 円 -2042.042 円 19,958 円\n入金期日 2026-11-25")
        g = invoice.guess(text, own_company="架空商事株式会社")
        self.assertEqual(g.amount, 19958)
        self.assertEqual(g.due_date, date(2026, 11, 25))
        self.assertEqual(g.payee, "サンプル会計事務所")

    def test_year_and_dates_not_amount_and_due_after_label(self):
        text = "株式会社DREAM&EFFECT 様\n株式会社サンプル不動産\nご請求金額 (税込) 120.000 円\n締日:2026年10月31日 お支払期限:2026年11月30日"
        g = invoice.guess(text, own_company="株式会社DREAM&EFFECT")
        self.assertEqual((g.amount, g.due_date, g.payee), (120000, date(2026, 11, 30), "株式会社サンプル不動産"))

    def test_known_payee_with_misread_char(self):
        g = invoice.guess("林岩運送株式会社\n合計金額 ¥38742", known_payees=["水岩運送株式会社"])
        self.assertEqual(g.payee, "水岩運送株式会社")


@override_settings(OCR_IN_BACKGROUND=False)
class RereadTests(TestCase):
    def test_reread_all_stuck(self):
        stepup.set_password("payroll", "pay-pass")
        self.client.post("/stepup/payroll/", {"password": "pay-pass", "next": "/payments/"})
        png = invoice_png(["株式会社サンプル物流", "ご請求金額 125,451円"])
        p = Payment.objects.create(month=NOV, status="draft", file=SimpleUploadedFile("a.png", png), original_name="a.png",
                                   error_message="読み取りが終わりませんでした。手で入力してください。")
        page = self.client.get("/payments/?month=2026-11")
        self.assertContains(page, "読み取れなかった 1件")
        self.client.post("/payments/reread-all/?month=2026-11")
        p.refresh_from_db()
        self.assertEqual(p.amount, 125451)
        self.assertEqual(p.error_message, "")
