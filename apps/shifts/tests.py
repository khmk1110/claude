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
        with self.assertRaises(attendance.MonthLockedError):  # 確定した月はシフト（＝勤怠）も変更できない
            services.set_shifts([self.a], [d], wt("off"))
        self.assertEqual(ShiftEntry.objects.get(member=self.a, date=d).work_type.code, "work")
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
        # シフトを見るだけならログイン不要（変更する画面・ボタンは出さない）
        for url in ["/shifts/", "/shifts/?view=day", "/shifts/?view=person", "/shifts/share/", "/shifts/image.png"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        page = self.client.get("/shifts/")
        self.assertNotContains(page, "LINEの数字でまとめて入力")
        self.assertContains(page, "ログイン")
        # 変更・勤怠・メンバーはログインが必要
        for url in ["/attendance/", "/members/", "/shifts/numbers/"]:
            self.assertIn("/login/", self.client.get(url)["Location"])
        res = self.client.post("/shifts/api/set/", data="{}", content_type="application/json")
        self.assertNotEqual(res.status_code, 200)


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


class MemberNumberingTests(TestCase):
    def test_auto_number_and_order_without_filling_gaps(self):
        a = Member.objects.create(name="一番 太郎", employment_type="contractor")
        b = Member.objects.create(name="二番 次郎", employment_type="intern")
        c = Member.objects.create(name="三番 三郎", employment_type="employee")
        self.assertEqual((a.member_no, b.member_no, c.member_no), ("1", "2", "3"))
        self.assertLess(a.display_order, b.display_order)
        self.assertLess(b.display_order, c.display_order)
        b.delete()  # 削除・退職しても番号は詰めない
        c.status = Member.Status.RETIRED
        c.save()
        d = Member.objects.create(name="四番 四郎", employment_type="contractor")
        self.assertEqual(d.member_no, "4")
        self.assertEqual(list(Member.objects.values_list("name", flat=True)), ["一番 太郎", "三番 三郎", "四番 四郎"])
        # 10 以降も数値として正しく続く
        for i in range(5, 12):
            Member.objects.create(name=f"メンバー{i}", employment_type="intern")
        self.assertEqual(Member.objects.create(name="最後", employment_type="intern").member_no, "12")


class ShiftImportFlowTests(TestCase):
    def make_book(self):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "シフト入力"
        ws["A1"], ws["B1"], ws["D1"], ws["E1"] = "年", 2026, "月", 11
        rows = [("青木", "休み", "1.4.8"), ("退職", "休み", "1,5"), ("矢野", "出勤日", "4、5、6"), ("井上", "出勤日", 21.22)]
        for i, r in enumerate(rows, start=4):
            for j, v in enumerate(r, start=1):
                ws.cell(i, j, v)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_unknown_and_ambiguous_names_can_be_resolved_on_screen(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(user)
        aoki = Member.objects.create(name="青木 一郎", employment_type="employee")
        yano1 = Member.objects.create(name="矢野 一郎", employment_type="contractor")
        Member.objects.create(name="矢野 次郎", employment_type="contractor")
        inoue = Member.objects.create(name="井上 三郎", employment_type="intern")
        self.client.post("/shifts/import/", {"step": "upload", "file": SimpleUploadedFile("s.xlsx", self.make_book())})
        page = self.client.get("/shifts/import/")
        self.assertContains(page, "同じ姓の人が複数います")
        self.assertContains(page, "登録されていない名前です")
        rows = {r["name"]: r for r in self.client.session["shift_import"]["rows"]}
        self.assertEqual(rows["青木"]["member_id"], aoki.pk)
        self.assertIsNone(rows["矢野"]["member_id"])
        res = self.client.post("/shifts/import/", {
            "step": "apply", "save_alias": "1",
            f"member_{rows['青木']['row']}": str(aoki.pk), f"member_{rows['退職']['row']}": "skip",
            f"member_{rows['矢野']['row']}": str(yano1.pk), f"member_{rows['井上']['row']}": str(inoue.pk)})
        self.assertRedirects(res, "/shifts/?month=2026-11", fetch_redirect_response=False)
        self.assertEqual(ShiftEntry.objects.filter(member=aoki, work_type__code="work").count(), 30 - 3)
        self.assertEqual(ShiftEntry.objects.filter(member=yano1, work_type__code="work").count(), 3)
        self.assertEqual(sorted(e.date.day for e in ShiftEntry.objects.filter(member=inoue, work_type__code="work")), [21, 22])
        self.assertTrue(MemberAlias.objects.filter(member=yano1, alias="矢野").exists())  # 次回から自動
        from apps.members.matching import match_member

        self.assertEqual(match_member("矢野").member, yano1)


class MemberFormTests(TestCase):
    def test_add_edit_and_delete_in_app(self):
        user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(user)
        self.client.post("/members/new/", {"name": "追加 太郎", "employment_type": "intern", "status": "active",
                                          "aliases": "追加\nついか たろう"})
        m = Member.objects.get(name="追加 太郎")
        self.assertEqual(m.member_no, "1")
        self.assertEqual(set(m.aliases.values_list("alias", flat=True)), {"追加", "ついか たろう"})
        res = self.client.post(f"/members/{m.pk}/edit/", {"name": "追加 太郎", "employment_type": "intern", "status": "retired"})
        self.assertContains(res, "退職日を入力してください")
        self.client.post(f"/members/{m.pk}/delete/")
        self.assertFalse(Member.objects.filter(pk=m.pk).exists())
        for url in ["/members/", "/members/new/", "/members/cards/", "/members/order/", "/shifts/import/", "/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class ShiftViewModesTests(TestCase):
    def test_day_view_and_blank_off(self):
        user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(user)
        m = Member.objects.create(name="表示 太郎", employment_type="intern")
        services.set_shifts([m], [date(2026, 11, 2)], wt("work"))
        services.set_shifts([m], [date(2026, 11, 3)], wt("off"))
        self.assertEqual(wt("off").short_label, "")  # 休みは空欄
        res = self.client.get("/shifts/?month=2026-11&view=day")
        self.assertContains(res, "日付別")
        self.assertContains(res, "表示 太郎")
        self.assertContains(self.client.get("/shifts/?month=2026-11"), "出勤人数")
        home = self.client.get("/")
        self.assertNotContains(home, "差引支給額")
        self.assertContains(home, "25日頃")


class PersonViewAndLockTests(TestCase):
    def test_person_calendar_and_lock_from_shift_page(self):
        user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(user)
        m = Member.objects.create(name="個人 太郎", employment_type="intern")
        res = self.client.get(f"/shifts/?month=2026-11&view=person&member={m.pk}")
        self.assertContains(res, "カレンダーで直す")
        self.assertContains(res, 'data-date="2026-11-01"')
        self.assertNotContains(res, "スマホ向け")
        api = lambda: self.client.post("/shifts/api/set/", data=json.dumps({"cells": [[m.pk, "2026-11-02"]], "work_type": "work"}),
                                       content_type="application/json")
        self.assertEqual(api().status_code, 200)
        res = self.client.post("/attendance/lock/", {"month": "2026-11", "next": "/shifts/?month=2026-11"})
        self.assertRedirects(res, "/shifts/?month=2026-11", fetch_redirect_response=False)
        self.assertContains(self.client.get("/shifts/?month=2026-11"), "確定済み")
        r = api()
        self.assertEqual(r.status_code, 400)
        self.assertIn("確定済み", r.json()["detail"])


class NumbersEntryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(self.user)
        self.a = Member.objects.create(name="出勤 太郎", employment_type="contractor")  # 出勤日を送ってくる人
        self.b = Member.objects.create(name="休み 花子", employment_type="employee", shift_input_mode="off")  # 休みを送ってくる人

    def post(self, **rows):
        data = {"month": "2026-11"}
        for m in (self.a, self.b):
            data.update({f"mode_{m.pk}": m.shift_input_mode, f"days_{m.pk}": "", f"initial_{m.pk}": "", f"initial_mode_{m.pk}": m.shift_input_mode})
        data.update(rows)
        return self.client.post("/shifts/numbers/?month=2026-11", data)

    def work_days(self, m):
        return sorted(e.date.day for e in ShiftEntry.objects.filter(member=m, work_type__code="work"))

    def test_line_numbers_both_modes_and_keep_half_days(self):
        services.set_shifts([self.a], [date(2026, 11, 7)], wt("half"))
        res = self.post(**{f"days_{self.a.pk}": "4、5.6 7", f"days_{self.b.pk}": "1,2,3"})
        self.assertRedirects(res, "/shifts/?month=2026-11", fetch_redirect_response=False)
        self.assertEqual(self.work_days(self.a), [4, 5, 6])  # 7日は半日のまま残す
        self.assertEqual(ShiftEntry.objects.get(member=self.a, date=date(2026, 11, 7)).work_type.code, "half")
        self.assertEqual(ShiftEntry.objects.filter(member=self.a, work_type__code="off").count(), 26)
        self.assertEqual(self.work_days(self.b), list(range(4, 31)))  # 休みモード: 書いた日以外が出勤
        page = self.client.get("/shifts/numbers/?month=2026-11")
        self.assertContains(page, 'value="4,5,6"')  # 入力済みの内容を表示

    def test_error_saves_nothing_and_mode_is_remembered(self):
        res = self.post(**{f"days_{self.a.pk}": "1,2", f"days_{self.b.pk}": "1,40"})
        self.assertContains(res, "まだ何も保存していません")
        self.assertFalse(ShiftEntry.objects.exists())
        self.post(**{f"mode_{self.a.pk}": "off", f"days_{self.a.pk}": "1"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.shift_input_mode, "off")

    def test_locked_month_is_read_only(self):
        from apps.attendance import services as attendance

        attendance.lock_month(date(2026, 11, 1))
        self.assertContains(self.client.get("/shifts/numbers/?month=2026-11"), "確定済み")
        self.post(**{f"days_{self.a.pk}": "1,2"})
        self.assertFalse(ShiftEntry.objects.exists())


class ShiftTabsTests(TestCase):
    def test_tabs_order_and_numbers_button(self):
        user = User.objects.create_user("u", password="x-pass-12345")
        self.client.force_login(user)
        html = self.client.get("/shifts/?month=2026-11").content.decode()
        tabs = html[html.index('<nav class="tabs">'):]
        tabs = tabs[:tabs.index("</nav>")]
        self.assertLess(tabs.index("シフト表"), tabs.index("日付別"))
        self.assertLess(tabs.index("日付別"), tabs.index("カレンダーで直す"))
        self.assertNotIn("LINEの数字で入力", tabs)
        self.assertIn("LINEの数字でまとめて入力", html)


class PublicTopPageTests(TestCase):
    def test_top_page_shows_shifts_without_login(self):
        res = self.client.get("/")
        self.assertEqual(res["Location"], "/shifts/")
        self.assertEqual(self.client.get("/shifts/").status_code, 200)
