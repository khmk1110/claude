from django.contrib.auth.hashers import identify_hasher
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts import stepup
from apps.accounts.models import StepUpPassword, User
from apps.core.models import AuditLog

PW = "test-pass-1"


class StepUpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("staff", password="login-pass-123")
        self.owner = User.objects.create_user("owner", password="login-pass-123", role=User.Role.OWNER)
        stepup.set_password("payroll", PW, by=self.owner)
        stepup.set_password("expenses", "another-pass", by=self.owner)
        self.client.force_login(self.user)

    def unlock(self, scope="payroll", password=PW):
        return self.client.post(reverse("stepup", args=[scope]), {"password": password, "next": f"/{scope}/"})

    def test_password_is_hashed_not_plaintext(self):
        stored = StepUpPassword.objects.get(scope="payroll").password_hash
        self.assertNotIn(PW, stored)
        identify_hasher(stored)  # 例外が出なければ Django のハッシュ形式

    def test_page_requires_stepup(self):
        res = self.client.get("/payroll/")
        self.assertRedirects(res, "/stepup/payroll/?next=%2Fpayroll%2F", fetch_redirect_response=False)

    def test_wrong_password_denied(self):
        res = self.unlock(password="wrong")
        self.assertEqual(res.status_code, 403)
        self.assertContains(res, "パスワードが正しくありません", status_code=403)
        self.assertEqual(self.client.get("/payroll/").status_code, 302)

    def test_correct_password_opens(self):
        self.assertRedirects(self.unlock(), "/payroll/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/payroll/").status_code, 200)
        self.assertTrue(AuditLog.objects.filter(action="stepup.success", username="staff").exists())

    def test_scopes_are_independent(self):
        self.unlock("payroll")
        self.assertEqual(self.client.get("/payroll/").status_code, 200)
        self.assertEqual(self.client.get("/expenses/").status_code, 302)
        self.unlock("expenses", "another-pass")
        self.assertEqual(self.client.get("/expenses/").status_code, 200)

    def test_api_and_downloads_get_403_json_without_stepup(self):
        res = self.client.get("/api/payroll/statements/", HTTP_ACCEPT="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["scope"], "payroll")
        # 存在しないURLでも、保護範囲内なら認証前に遮断する（URLの存在すら漏らさない）
        self.assertEqual(self.client.post("/payroll/anything/export.csv").status_code, 403)

    def test_admin_payroll_section_blocked(self):
        admin = User.objects.create_superuser("admin", password="login-pass-123")
        self.client.force_login(admin)
        res = self.client.get("/admin/payroll/payrollstatement/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/stepup/payroll/", res["Location"])

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        res = self.client.get("/payroll/")
        self.assertIn("/login/", res["Location"])

    def test_logout_revokes_stepup(self):
        self.unlock()
        self.client.post(reverse("logout"))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/payroll/").status_code, 302)

    @override_settings(STEPUP_MAX_FAILURES=3)
    def test_lockout_after_consecutive_failures(self):
        for _ in range(3):
            self.unlock(password="wrong")
        res = self.unlock()  # 正しいパスワードでもロック中は通さない
        self.assertContains(res, "まで入力できません", status_code=403)
        self.assertEqual(self.client.get("/payroll/").status_code, 302)

    @override_settings(STEPUP_IDLE_SECONDS=0)
    def test_idle_timeout_requires_reauth(self):
        self.unlock()
        self.assertEqual(self.client.get("/payroll/").status_code, 302)

    def test_unconfigured_scope_cannot_be_opened(self):
        StepUpPassword.objects.filter(scope="expenses").delete()
        res = self.unlock("expenses", "")
        self.assertEqual(self.client.get("/expenses/").status_code, 302)
        self.assertNotEqual(res.status_code, 302)

    def test_only_owner_can_change_passwords(self):
        self.assertEqual(self.client.get(reverse("stepup_password_change")).status_code, 403)
        self.client.force_login(self.owner)
        res = self.client.post(
            reverse("stepup_password_change"),
            {"scope": "payroll", "new_password": "newpass", "new_password_confirm": "newpass"},
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(AuditLog.objects.filter(action="stepup.password_changed").exists())
        self.client.force_login(self.user)
        self.assertEqual(self.unlock(password=PW).status_code, 403)
        self.assertEqual(self.unlock(password="newpass").status_code, 302)

    def test_open_redirect_rejected(self):
        res = self.client.post(reverse("stepup", args=["payroll"]), {"password": PW, "next": "https://evil.example/"})
        self.assertEqual(res["Location"], "/payroll/")


class StepUpCoverageTests(TestCase):
    """給与・経費の全 URL が、追加認証なしでは中身を返さないことを網羅的に確認する。"""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", password="login-pass-123")
        stepup.set_password("payroll", PW)
        stepup.set_password("expenses", "exp-pass")
        self.client.force_login(self.user)

    def all_urls(self, module):
        from django.urls import get_resolver
        from django.urls.resolvers import URLPattern

        urls = []
        for p in get_resolver(module).url_patterns:
            if isinstance(p, URLPattern):
                route = str(p.pattern)
                route = route.replace("<int:pk>", "1").replace("<int:page>", "1").replace("<str:fmt>", "csv")
                urls.append(route)
        return urls

    def test_every_payroll_and_expense_url_is_gated(self):
        for prefix, module in (("/payroll/", "apps.payroll.urls"), ("/expenses/", "apps.expenses.urls")):
            for route in self.all_urls(module):
                url = prefix + route
                for method in ("get", "post"):
                    res = getattr(self.client, method)(url)
                    self.assertIn(res.status_code, (302, 403), f"{method.upper()} {url}")
                    if res.status_code == 302:
                        self.assertIn("/stepup/", res["Location"], url)

    def test_admin_autocomplete_does_not_leak(self):
        from apps.members.models import Member
        from apps.payroll.models import PayrollStatement

        m = Member.objects.create(member_no="X1", name="検索 太郎", employment_type="contractor")
        PayrollStatement.objects.create(member=m, month="2026-10-01", employment_type="contractor", net_amount=123456)
        # 給与データ（PayrollStatement）を検索対象にする経路は遮断される
        res = self.client.get("/admin/autocomplete/", {"app_label": "expenses", "model_name": "expenserecord",
                                                        "field_name": "reflected_statement", "term": ""})
        self.assertNotEqual(res.status_code, 200)
        # メンバー名（給与画面以外でも見られる情報）を返す経路でも、金額は含まれない
        res = self.client.get("/admin/autocomplete/", {"app_label": "payroll", "model_name": "payrollstatement",
                                                        "field_name": "member", "term": ""})
        self.assertNotIn(b"123456", res.content)

    def test_close_revokes(self):
        self.client.post("/stepup/payroll/", {"password": PW})
        self.assertEqual(self.client.get("/payroll/").status_code, 200)
        self.client.post("/stepup/payroll/close/")
        self.assertEqual(self.client.get("/payroll/").status_code, 302)


class BootstrapAndSetupTests(TestCase):
    def test_bootstrap_creates_once_and_never_overwrites(self):
        import os
        from unittest import mock

        from django.core.management import call_command

        env = {"INITIAL_ADMIN_USERNAME": "owner", "INITIAL_ADMIN_PASSWORD": "first-pass-123",
               "INITIAL_PAYROLL_PASSWORD": "pay-1", "INITIAL_EXPENSES_PASSWORD": "exp-1"}
        with mock.patch.dict(os.environ, env):
            call_command("bootstrap", stdout=open(os.devnull, "w"))
        owner = User.objects.get(username="owner")
        self.assertEqual(owner.role, User.Role.OWNER)
        stepup.set_password("payroll", "changed")  # 画面で変更した後に再起動しても
        with mock.patch.dict(os.environ, {**env, "INITIAL_ADMIN_USERNAME": "other"}):
            call_command("bootstrap", stdout=open(os.devnull, "w"))
        self.assertFalse(User.objects.filter(username="other").exists())
        from django.contrib.auth.hashers import check_password

        self.assertTrue(check_password("changed", StepUpPassword.objects.get(scope="payroll").password_hash))

    def test_setup_and_backup_permissions(self):
        staff = User.objects.create_user("staff", password="login-pass-123")
        owner = User.objects.create_user("owner", password="login-pass-123", role=User.Role.OWNER)
        stepup.set_password("payroll", PW)
        stepup.set_password("expenses", "exp-pass")
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/settings/").status_code, 403)
        self.assertEqual(self.client.post("/settings/backup/").status_code, 403)
        self.client.force_login(owner)
        self.assertEqual(self.client.get("/settings/").status_code, 200)
        res = self.client.post("/settings/backup/")  # 追加認証なし → ダウンロードさせない
        self.assertEqual(res.status_code, 302)
        self.client.post("/stepup/payroll/", {"password": PW})
        self.assertEqual(self.client.post("/settings/backup/").status_code, 302)  # 経費の認証がまだ
        self.client.post("/stepup/expenses/", {"password": "exp-pass"})
        res = self.client.post("/settings/backup/")
        self.assertEqual(res["Content-Type"], "application/gzip")
        import gzip
        import json

        data = json.loads(gzip.decompress(res.content))
        self.assertTrue(any(o["model"] == "accounts.stepuppassword" for o in data))
        self.assertTrue(AuditLog.objects.filter(action="setup.backup").exists())


class RestoreTests(TestCase):
    def test_restore_rejected_when_data_exists_and_backup_roundtrip(self):
        import gzip
        import io as _io
        import json

        from django.core.management import call_command

        from apps.core.management.commands.restore_backup import restore
        from apps.members.models import Member

        Member.objects.create(member_no="R1", name="復元 太郎", employment_type="contractor")
        buf = _io.StringIO()
        call_command("dumpdata", "members.member", stdout=buf)
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json.gz", delete=False) as f:
            f.write(gzip.compress(buf.getvalue().encode()))
        from django.core.management import CommandError

        with self.assertRaises(CommandError):  # データがある DB には復元しない
            restore(f.name)
        Member.objects.all().delete()
        restore(f.name)
        self.assertTrue(Member.objects.filter(name="復元 太郎").exists())
        self.assertEqual(len(json.loads(buf.getvalue())), 1)
