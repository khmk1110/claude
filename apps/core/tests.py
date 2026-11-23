from datetime import date

from django.db import IntegrityError, InternalError, transaction
from django.test import TestCase

from apps.core import audit
from apps.core.models import AuditLog
from apps.core.months import month_end, parse_month, prev_month
from apps.core.text import normalize_name


class AuditLogTests(TestCase):
    def test_audit_log_cannot_be_updated_or_deleted_at_db_level(self):
        entry = audit.log("test.action", target_type="x", target_id=1, after={"a": 1})
        for op in (
            lambda: AuditLog.objects.filter(pk=entry.pk).update(action="tampered"),
            lambda: AuditLog.objects.filter(pk=entry.pk).delete(),
        ):
            with self.assertRaises((InternalError, IntegrityError, Exception)), transaction.atomic():
                op()
        self.assertEqual(AuditLog.objects.get(pk=entry.pk).action, "test.action")


class UtilTests(TestCase):
    def test_normalize_name(self):
        self.assertEqual(normalize_name("関    佑翔"), "関佑翔")
        self.assertEqual(normalize_name("関　佑翔"), "関佑翔")
        self.assertEqual(normalize_name("ＡＢＣ 1"), "ABC1")

    def test_months(self):
        self.assertEqual(prev_month(date(2026, 1, 1)), date(2025, 12, 1))
        self.assertEqual(month_end(date(2028, 2, 1)), date(2028, 2, 29))
        self.assertEqual(parse_month("2026/10"), date(2026, 10, 1))


class SetupFileTests(TestCase):
    def setUp(self):
        from apps.members.models import Member

        self.M = Member
        for name, emp in (("一ノ瀬 太郎", "contractor"), ("二宮 花子", "contractor"), ("三田 次郎", "intern"), ("辞退 四郎", "intern")):
            Member.objects.create(name=name, employment_type=emp)

    def data(self):
        return {
            "members": [
                {"name": "役員 一郎", "employment_type": "employee", "member_no": "1", "paid_leave_target": False, "note": "役員"},
                {"name": "一ノ瀬 太郎", "member_no": "2"},
                {"name": "二宮 花子", "member_no": "3"},
                {"name": "三田 次郎", "member_no": "4", "aliases": ["三田 じろう"]},
                {"name": "新人 五郎", "employment_type": "intern", "member_no": "5", "joined_on": "2026-07-18"},
                {"name": "辞退 四郎", "delete": True},
            ],
            "order": ["役員 一郎", "一ノ瀬 太郎", "二宮 花子", "三田 次郎", "新人 五郎"],
            "cards": [{"code": "0003", "member": "一ノ瀬 太郎"}, {"code": "0001", "ignored": True, "label": "役員"}],
            "teams": [{"name": "一ノ瀬チーム", "kind": "sales", "leader": "一ノ瀬 太郎", "members": ["二宮 花子"], "start_month": "2026-01"}],
            "items": [{"code": "consult", "name": "コンサル", "kind": "earning", "method": "manual", "scope": "member",
                       "members": ["一ノ瀬 太郎"], "default_amount": 0, "display_order": 16}],
            "item_members": {"product_houmon_denki": ["一ノ瀬 太郎"]},
            "item_excluded": {"sales_commission": ["一ノ瀬 太郎"]},
        }

    def test_dry_run_changes_nothing_and_apply_registers_everything(self):
        from apps.core.setup_file import apply_setup
        from apps.members.models import CardCode
        from apps.payroll.models import PayrollItemDefinition, Team

        log = apply_setup(self.data(), dry_run=True)
        self.assertIn("（確認のみ。まだ登録していません）", log)
        self.assertEqual(self.M.objects.count(), 4)
        apply_setup(self.data(), dry_run=False)
        nums = dict(self.M.objects.values_list("name", "member_no"))
        self.assertEqual(nums, {"役員 一郎": "1", "一ノ瀬 太郎": "2", "二宮 花子": "3", "三田 次郎": "4", "新人 五郎": "5"})
        self.assertEqual(list(self.M.objects.values_list("name", flat=True))[0], "役員 一郎")
        self.assertEqual(self.M.objects.get(name="新人 五郎").joined_on.isoformat(), "2026-07-18")
        self.assertEqual(CardCode.objects.get(code="0003").member.name, "一ノ瀬 太郎")
        self.assertTrue(CardCode.objects.get(code="0001").ignored)
        team = Team.objects.get(name="一ノ瀬チーム")
        self.assertEqual([m.name for m in team.members_for(date(2026, 11, 1))], ["二宮 花子"])
        self.assertTrue(PayrollItemDefinition.objects.get(code="sales_commission").excluded_members.filter(name="一ノ瀬 太郎").exists())
        self.assertTrue(PayrollItemDefinition.objects.get(code="consult").members.filter(name="一ノ瀬 太郎").exists())
        self.assertEqual(self.M.objects.create(name="次 六郎", employment_type="intern").member_no, "6")  # 以降は自動で続く
        apply_setup(self.data() | {"members": [m for m in self.data()["members"] if not m.get("delete")]}, dry_run=False)  # 再実行しても重複しない
        self.assertEqual(self.M.objects.filter(name="新人 五郎").count(), 1)

    def test_error_rolls_back(self):
        from apps.core.setup_file import SetupError, apply_setup

        bad = {"members": [{"name": "新人 五郎", "employment_type": "intern", "member_no": "9"}],
               "teams": [{"name": "X", "leader": "存在しない 人", "members": []}]}
        with self.assertRaises(SetupError):
            apply_setup(bad, dry_run=False)
        self.assertFalse(self.M.objects.filter(name="新人 五郎").exists())


class TeamPageTests(TestCase):
    def test_team_edit_reflects_in_payroll(self):
        from apps.accounts import stepup
        from apps.accounts.models import User
        from apps.members.models import Member
        from apps.payroll import calc
        from apps.payroll.models import PayrollStatement, SalesCount, Team
        from apps.payroll.tests import make_tables

        make_tables()
        user = User.objects.create_user("o", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        self.client.force_login(user)
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        leader = Member.objects.create(name="長 太郎", employment_type="contractor")
        a = Member.objects.create(name="員 一郎", employment_type="contractor")
        b = Member.objects.create(name="員 二郎", employment_type="employee")
        for m, n in ((leader, 100), (a, 90), (b, 50)):
            SalesCount.objects.create(member=m, month=date(2026, 11, 1), kind="confirmed", count=n)
            SalesCount.objects.create(member=m, month=date(2026, 10, 1), kind="acquired", count=n)  # ボーナスは前月の獲得件数で判定
        self.client.post("/payroll/teams/", {"action": "create", "kind": "sales", "leader": leader.pk, "name": "長チーム"})
        team = Team.objects.get(name="長チーム")
        self.client.post("/payroll/teams/", {"action": "add", "team": team.pk, "member": a.pk, "start_month": "2026-11"})
        calc.prepare_month(date(2026, 11, 1))
        st = PayrollStatement.objects.get(member=leader)
        st = calc.calculate(st)
        self.assertEqual(st.lines.get(code="team_bonus").amount, 50000 * 1)  # 平均95件 → 5万円 × 1名
        self.client.post("/payroll/teams/", {"action": "add", "team": team.pk, "member": b.pk, "start_month": "2026-11"})
        st = calc.calculate(st)
        self.assertEqual(st.lines.get(code="team_bonus").amount, 40000 * 2)  # 平均80件 → 4万円 × 2名
        self.assertEqual(self.client.get("/payroll/teams/").status_code, 200)


class SetupPasteTests(TestCase):
    def test_paste_text_on_setup_page(self):
        import json as _json

        from apps.accounts import stepup
        from apps.accounts.models import User
        from apps.members.models import Member

        owner = User.objects.create_user("o", password="x-pass-12345", role=User.Role.OWNER)
        stepup.set_password("payroll", "pay-pass")
        self.client.force_login(owner)
        text = _json.dumps({"members": [{"name": "貼付 太郎", "employment_type": "intern", "member_no": "1"}]}, ensure_ascii=False)
        res = self.client.post("/settings/", {"action": "setupfile", "text": text})
        self.assertNotEqual(res.status_code, 200)  # 設定は給与のパスワードが先
        self.client.post("/stepup/payroll/", {"password": "pay-pass"})
        res = self.client.post("/settings/", {"action": "setupfile", "text": text})
        self.assertContains(res, "確認のみ")
        self.assertFalse(Member.objects.exists())
        self.client.post("/settings/", {"action": "setupfile", "text": text, "apply": "1"})
        self.assertEqual(Member.objects.get().member_no, "1")


class CsrfFailureTests(TestCase):
    def test_stale_login_form_is_reopened_and_pages_are_not_cached(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        c.get("/login/")
        res = c.post("/login/", {"username": "x", "password": "y", "csrfmiddlewaretoken": "stale-token-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"})
        self.assertRedirects(res, "/login/", fetch_redirect_response=False)
        page = c.get("/login/")
        self.assertContains(page, "もう一度ログインしてください")
        self.assertIn("no-store", page["Cache-Control"])


class HealthzTests(TestCase):
    def test_healthz_does_not_touch_database(self):
        # スリープ防止で数分おきに呼ばれても、無料のデータベースを起こさない
        with self.assertNumQueries(0):
            res = self.client.get("/healthz")
        self.assertEqual(res.content, b"ok")
        self.assertEqual(self.client.get("/healthz?db=1").status_code, 200)
