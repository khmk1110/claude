from datetime import date

from django.test import TestCase

from apps.attendance.models import AttendanceDay
from apps.members.models import Member
from apps.shifts.models import WorkType


class AttendanceTests(TestCase):
    def test_work_types_seeded_with_conversion_rules(self):
        rules = {w.code: (float(w.attendance_days), float(w.paid_leave_days)) for w in WorkType.objects.all()}
        self.assertEqual(
            rules,
            {"work": (1, 0), "half": (0.5, 0), "off": (0, 0), "paid": (0, 1), "half_paid": (0.5, 0.5)},
        )

    def test_planned_follows_shift_confirmed_does_not(self):
        m = Member.objects.create(member_no="T1", name="テスト", employment_type="employee")
        work, off = WorkType.objects.get(code="work"), WorkType.objects.get(code="off")
        day = AttendanceDay.objects.create(member=m, date=date(2026, 11, 3), planned_work_type=work)
        self.assertEqual(day.effective_work_type, work)
        day.status, day.actual_work_type = AttendanceDay.Status.CONFIRMED, work
        day.planned_work_type = off  # シフトが後から変わっても
        self.assertEqual(day.effective_work_type, work)  # 確定済みの実績は変わらない
        self.assertTrue(day.differs_from_plan)
