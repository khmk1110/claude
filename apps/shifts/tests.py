import json
from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.members.models import Member, MemberAlias
from apps.shifts import services
from apps.shifts.image import render_png
from apps.shifts.legacy import DayListError, parse_day_list
from apps.shifts.models import ShiftEntry, WorkType

NOV = date(2026, 11, 1)


def wt(code):
    return WorkType.objects.get(code=code)


class ParseDayListTests(TestCase):
    def test_separators(self):
        self.assertEqual(parse_day_list("1,5、6．12 13", 30), [1, 5, 6, 12, 13])
        self.assertEqual(parse_day_list("4.10.11", 30), [4, 10, 11])
        self.assertEqual(parse_day_list(18.0, 30), [18])
        self.assertEqual(parse_day_list(21.22, 30), [21, 22])
        self.assertEqual(parse_day_list("3.4.5..7", 30), [3, 4, 5, 7])  # 区切りの重複は許容

    def test_errors_are_not_guessed(self):
        with self.assertRaises(DayListError):
            parse_day_list("31", 30)
        with self.assertRaises(DayListError):
            parse_day_list("1,a", 30)
        with self.assertRaises(DayListError):
            parse_day_list(21.2, 30)  # 21.20 が数値化で欠けた可能性


class ShiftServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", password="x-pass-12345")
        self.a = Member.objects.create(member_no="1", name="山田 一郎", employment_type="contractor", display_order=1)
        self.b = Member.objects.create(member_no="2", name="山田 二郎", employment_type="employee", display_order=2)

    def test_set_shift_syncs_attendance_and_logs(self):
        d = date(2026, 11, 7)  # 土曜日でも通常どおり入力できる
        services.set_shifts([self.a], [d], wt("work"), user=self.user)
        day = AttendanceDay.objects.get(member=self.a, date=d)
        self.assertEqual(day.planned_work_type.code, "work")
        log = AuditLog.objects.get(action="shift.update")
        self.assertEqual((log.before, log.after, log.username), ({"work_type": None}, {"work_type": "出勤"}, "u"))
        # 同じ値を入れても履歴は増えない
        self.assertEqual(services.set_shifts([self.a], [d], wt("work"), user=self.user), 0)

    def test_shift_change_does_not_overwrite_confirmed_attendance(self):
        d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
        services.set_shifts([self.a], [d1, d2], wt("work"))
        attendance.update_actual(self.a, d1, work_type=wt("half"), user=self.user, reason="午後のみ")
        services.set_shifts([self.a], [d1, d2], wt("off"))
        day1, day2 = (AttendanceDay.objects.get(member=self.a, date=d) for d in (d1, d2))
        self.assertEqual(day1.effective_work_type.code, "half")  # 修正済み → 上書きしない
        self.assertEqual(day1.planned_work_type.code, "off")  # 予定は更新（差異として表示）
        self.assertTrue(day1.differs_from_plan)
        self.assertEqual(day2.effective_work_type.code, "off")  # 未確定 → 追従

    def test_locked_month_is_not_changed(self):
        d = date(2026, 11, 4)
        services.set_shifts([self.a], [d], wt("work"))
        attendance.lock_month(NOV)
        services.set_shifts([self.a], [d], wt("off"))
        day = AttendanceDay.objects.get(member=self.a, date=d)
        self.assertEqual(day.effective_work_type.code, "work")
        with self.assertRaises(attendance.MonthLockedError):
            attendance.update_actual(self.a, d, work_type=wt("off"))

    def test_summary_conversion(self):
        services.set_shifts([self.a], [date(2026, 11, 2), date(2026, 11, 3)], wt("work"))
        services.set_shifts([self.a], [date(2026, 11, 4)], wt("half"))
        services.set_shifts([self.a], [date(2026, 11, 5)], wt("paid"))
        services.set_shifts([self.a], [date(2026, 11, 6)], wt("half_paid"))
        services.set_shifts([self.a], [date(2026, 11, 7)], wt("off"))
        s = attendance.month_summaries([self.a], NOV)[self.a.pk]
        self.assertEqual((s.attendance_days, s.paid_leave_days), (Decimal("3.0"), Decimal("1.5")))
        row = services.month_grid(NOV)[0]
        self.assertEqual(row.planned_days, Decimal("3.0"))
        self.assertEqual(row.standard, Decimal("20"))
        self.assertEqual(row.diff, Decimal("-17.0"))
        days, source = attendance.actual_attendance_days(self.a, NOV)
        self.assertEqual(days, Decimal("3.0"))
        self.assertEqual(attendance.actual_attendance_days(self.b, NOV), (None, ""))  # データなし → 手入力

    def test_copy_previous_month_fills_only_empty(self):
        services.set_shifts([self.a], [date(2026, 10, 1), date(2026, 10, 31)], wt("work"))
        services.set_shifts([self.a], [date(2026, 11, 1)], wt("off"))
        n = services.copy_previous_month(NOV)
        self.assertEqual(n, 0)  # 11/1 は入力済み、10/31 は11月にない
        services.set_shifts([self.a], [date(2026, 10, 2)], wt("half"))
        self.assertEqual(services.copy_previous_month(NOV), 1)
        self.assertEqual(ShiftEntry.objects.get(member=self.a, date=date(2026, 11, 2)).work_type.code, "half")
        self.assertEqual(ShiftEntry.objects.get(member=self.a, date=date(2026, 11, 1)).work_type.code, "off")

    def test_retired_members_hidden_after_retirement_month(self):
        self.b.status, self.b.retired_on = Member.Status.RETIRED, date(2026, 10, 15)
        self.b.save()
        self.assertIn(self.b, services.members_for_month(date(2026, 10, 1)))
        self.assertNotIn(self.b, services.members_for_month(NOV))

    def test_png_render(self):
        services.set_shifts([self.a], [date(2026, 11, 2)], wt("work"))
        png = render_png(NOV, "テスト株式会社")
        self.assertTrue(png.startswith(b"\x89PNG"))
        from io import BytesIO

        from PIL import Image

        w, h = Image.open(BytesIO(png)).size
        self.assertGreater(w, h)  # 横長
        self.assertGreaterEqual(w, 2400)  # 高解像度


class ShiftViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", password="x-pass-12345")
        self.m = Member.objects.create(member_no="1", name="テスト 太郎", employment_type="intern")
        self.client.force_login(self.user)

    def test_grid_and_api(self):
        self.assertEqual(self.client.get("/shifts/?month=2026-11").status_code, 200)
        res = self.client.post(
            "/shifts/api/set/",
            data=json.dumps({"cells": [[self.m.pk, "2026-11-01"], [self.m.pk, "2026-11-02"]], "work_type": "work"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["rows"][str(self.m.pk)]["planned"], "2")
        res = self.client.post("/shifts/api/set/", data=json.dumps({"cells": [[self.m.pk, "2026-11-01"]], "work_type": None}),
                               content_type="application/json")
        self.assertEqual(res.json()["rows"][str(self.m.pk)]["planned"], "1")
        bad = self.client.post("/shifts/api/set/", data=json.dumps({"cells": [[999, "2026-11-01"]], "work_type": "work"}),
                               content_type="application/json")
        self.assertEqual(bad.status_code, 400)

    def test_pages_render(self):
        for url in ["/shifts/share/?month=2026-11", "/shifts/history/?month=2026-11", "/members/", "/members/order/",
                    "/attendance/?month=2026-11", f"/attendance/{self.m.pk}/?month=2026-11", "/attendance/leave/",
                    f"/attendance/leave/{self.m.pk}/", "/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        res = self.client.get("/shifts/image.png?month=2026-11&download=1")
        self.assertEqual(res["Content-Type"], "image/png")

    def test_login_required(self):
        self.client.logout()
        for url in ["/shifts/", "/attendance/", "/members/", "/shifts/image.png"]:
            self.assertIn("/login/", self.client.get(url)["Location"])


class MatchingTests(TestCase):
    def test_surname_only_ambiguous_requires_alias(self):
        from apps.members.matching import match_member

        a = Member.objects.create(member_no="1", name="矢野 一郎", employment_type="contractor")
        Member.objects.create(member_no="2", name="矢野 次郎", employment_type="contractor")
        self.assertEqual(match_member("矢野").method, "ambiguous")
        MemberAlias.objects.create(member=a, alias="矢野")
        self.assertEqual(match_member("矢野").member, a)
        self.assertEqual(match_member("矢野　一郎").member, a)


class MemberDeleteTests(TestCase):
    def test_only_members_without_history_can_be_deleted(self):
        admin = User.objects.create_superuser("root", password="x-pass-12345")
        self.client.force_login(admin)
        fresh = Member.objects.create(member_no="N1", name="新規 太郎", employment_type="contractor")
        used = Member.objects.create(member_no="N2", name="記録 次郎", employment_type="contractor")
        services.set_shifts([used], [date(2026, 11, 2)], wt("work"))
        self.assertFalse(fresh.has_history())
        self.assertTrue(used.has_history())
        self.assertEqual(self.client.post(f"/admin/members/member/{used.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.client.post(f"/admin/members/member/{fresh.pk}/delete/", {"post": "yes"})
        self.assertFalse(Member.objects.filter(pk=fresh.pk).exists())
        self.assertTrue(Member.objects.filter(pk=used.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="member.delete").exists())
