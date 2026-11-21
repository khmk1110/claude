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
