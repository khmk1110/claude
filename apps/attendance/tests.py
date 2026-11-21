from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.attendance import leave
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceDay, PaidLeaveGrant, PaidLeaveRequest
from apps.members.models import Member
from apps.shifts import services as shifts
from apps.shifts.models import WorkType


def wt(code):
    return WorkType.objects.get(code=code)


class AttendanceTests(TestCase):
    def test_work_types_seeded_with_conversion_rules(self):
        rules = {w.code: (float(w.attendance_days), float(w.paid_leave_days)) for w in WorkType.objects.all()}
        self.assertEqual(
            rules,
            {"work": (1, 0), "half": (0.5, 0), "off": (0, 0), "paid": (0, 1), "half_paid": (0.5, 0.5)},
        )

    def test_lock_month_confirms_pending_days(self):
        m = Member.objects.create(member_no="T1", name="テスト", employment_type="employee")
        shifts.set_shifts([m], [date(2026, 11, 3)], wt("work"))
        attendance.lock_month(date(2026, 11, 1))
        day = AttendanceDay.objects.get(member=m)
        self.assertEqual((day.status, day.actual_work_type.code), ("confirmed", "work"))
        with self.assertRaises(ValueError):
            attendance.unlock_month(date(2026, 11, 1), reason="")
        attendance.unlock_month(date(2026, 11, 1), reason="修正のため")
        self.assertFalse(attendance.is_locked(date(2026, 11, 1)))


class LeaveTests(TestCase):
    def setUp(self):
        self.m = Member.objects.create(
            member_no="E1", name="社員 太郎", employment_type="employee", joined_on=date(2024, 4, 1), paid_leave_target=True
        )

    def test_statutory_schedule(self):
        g = leave.statutory_grants(date(2024, 4, 1), date(2030, 12, 31))
        self.assertEqual(g[0], (date(2024, 10, 1), 10))
        self.assertEqual(g[1], (date(2025, 10, 1), 11))
        self.assertEqual([d for _, d in g], [10, 11, 12, 14, 16, 18, 20])
        self.assertEqual(leave.expiry_for(date(2024, 10, 1)), date(2026, 9, 30))
        self.assertEqual(leave.add_months(date(2024, 8, 31), 6), date(2025, 2, 28))

    def test_missing_statutory_grants_are_flagged(self):
        st = leave.leave_status(self.m, date(2026, 11, 21))
        self.assertEqual([g for g, _ in st.missing_statutory], [date(2024, 10, 1), date(2025, 10, 1), date(2026, 10, 1)])
        leave.create_statutory_grants(self.m, until=date(2026, 11, 21))
        self.assertEqual(leave.leave_status(self.m, date(2026, 11, 21)).missing_statutory, [])

    def test_balance_fifo_planned_requested_and_obligation(self):
        n = leave.create_statutory_grants(self.m, until=date(2026, 11, 21))
        self.assertEqual(n, 3)  # 2024/10/1 10日, 2025/10/1 11日, 2026/10/1 12日
        shifts.set_shifts([self.m], [date(2026, 1, 5), date(2026, 1, 6)], wt("paid"))
        shifts.set_shifts([self.m], [date(2026, 1, 7)], wt("half_paid"))
        attendance.lock_month(date(2026, 1, 1))  # 取得済みにする
        shifts.set_shifts([self.m], [date(2026, 12, 1)], wt("paid"))  # 予定
        PaidLeaveRequest.objects.create(member=self.m, date=date(2026, 12, 10), days=Decimal("1"))  # 申請中
        st = leave.leave_status(self.m, date(2026, 11, 21))
        self.assertEqual(st.taken, Decimal("2.5"))
        self.assertEqual(st.planned, Decimal("1"))
        self.assertEqual(st.requested, Decimal("1"))
        # 2024付与10日は古い順に2.5日消化 → 2026/9/30で残り7.5日は時効。2025付与11日＋2026付与12日が残る
        self.assertEqual(st.remaining, Decimal("23"))
        self.assertEqual(st.remaining_after_planned, Decimal("22"))
        self.assertEqual(st.grants[0].used, Decimal("2.5"))
        o = st.obligation  # 直近の10日以上の付与（2026/10/1）から1年
        self.assertEqual((o["start"], o["deadline"]), (date(2026, 10, 1), date(2027, 9, 30)))
        self.assertEqual((o["taken"], o["planned"], o["shortfall"]), (Decimal("0"), Decimal("1"), Decimal("4")))
        self.assertFalse(o["achieved"])

    def test_requested_leave_not_counted_until_approved(self):
        PaidLeaveGrant.objects.create(member=self.m, granted_on=date(2026, 10, 1), days=10, expires_on=date(2028, 9, 30))
        req = PaidLeaveRequest.objects.create(member=self.m, date=date(2026, 11, 25), days=Decimal("0.5"))
        st = leave.leave_status(self.m, date(2026, 11, 21))
        self.assertEqual((st.planned, st.requested), (Decimal("0"), Decimal("0.5")))
        leave.decide_request(req, approve=True)
        st = leave.leave_status(self.m, date(2026, 11, 21))
        self.assertEqual((st.planned, st.requested), (Decimal("0.5"), Decimal("0")))
        self.assertEqual(AttendanceDay.objects.get(member=self.m).planned_work_type.code, "half_paid")
