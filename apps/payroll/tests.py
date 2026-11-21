from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from apps.core.models import ImmutableAfterConfirmError
from apps.members.models import EmploymentType, Member
from apps.payroll.models import PayrollLine, PayrollStatement, RateTable

# テスト用の架空の単価表（実際の単価は公開リポジトリに置かない）
DAY_BOUNDS = [0, 11, 16]
COUNT_BOUNDS = [0, 40, 50]
PRICES = [[100, 110, 120], [200, 210, 220], [300, 310, 320]]


class RateTableTests(TestCase):
    def setUp(self):
        self.table = RateTable(
            name="テスト",
            employment_type=EmploymentType.CONTRACTOR,
            effective_from=date(2026, 1, 1),
            day_lower_bounds=DAY_BOUNDS,
            count_lower_bounds=COUNT_BOUNDS,
            prices=PRICES,
        )
        self.table.full_clean()

    def test_boundaries(self):
        self.assertEqual(self.table.lookup(0, 0), 100)
        self.assertEqual(self.table.lookup(10, 39), 100)
        self.assertEqual(self.table.lookup(11, 40), 210)
        self.assertEqual(self.table.lookup(15, 49), 210)
        self.assertEqual(self.table.lookup(16, 50), 320)
        self.assertEqual(self.table.lookup(31, 999), 320)

    def test_half_days_are_floored(self):
        # 10.5日 → 10日として判定（切り捨て）
        self.assertEqual(self.table.lookup(Decimal("10.5"), 45), 110)
        self.assertEqual(self.table.lookup(Decimal("15.5"), 45), 210)

    def test_missing_previous_data_raises(self):
        with self.assertRaises(ValueError):
            self.table.lookup(None, 10)
        with self.assertRaises(ValueError):
            self.table.lookup(10, None)

    def test_shape_validation(self):
        self.table.prices = [[1, 2], [3, 4]]
        with self.assertRaises(ValidationError):
            self.table.full_clean()
        self.table.prices = PRICES
        self.table.day_lower_bounds = [1, 11, 16]
        with self.assertRaises(ValidationError):
            self.table.full_clean()

    def test_for_month_picks_effective_version(self):
        self.table.save()
        RateTable.objects.create(
            name="改定後",
            employment_type=EmploymentType.CONTRACTOR,
            effective_from=date(2026, 7, 1),
            day_lower_bounds=DAY_BOUNDS,
            count_lower_bounds=COUNT_BOUNDS,
            prices=PRICES,
        )
        self.assertEqual(RateTable.for_month(EmploymentType.CONTRACTOR, date(2026, 6, 1)).name, "テスト")
        self.assertEqual(RateTable.for_month(EmploymentType.CONTRACTOR, date(2026, 7, 1)).name, "改定後")
        self.assertIsNone(RateTable.for_month(EmploymentType.INTERN, date(2026, 7, 1)))


class ConfirmedStatementTests(TestCase):
    def setUp(self):
        self.member = Member.objects.create(member_no="T001", name="テスト 太郎", employment_type="contractor")
        self.st = PayrollStatement.objects.create(
            member=self.member, month=date(2026, 10, 1), employment_type="contractor"
        )
        self.line = PayrollLine.objects.create(statement=self.st, name="営業報酬", kind="earning", auto_amount=1000)
        self.st.earnings_total = self.st.net_amount = 1000
        self.st.deductions_total = 0
        self.st.status = PayrollStatement.Status.CONFIRMED
        self.st.snapshot = {"lines": [{"name": "営業報酬", "amount": 1000}]}
        self.st.save()

    def test_app_level_guard(self):
        self.st.net_amount = 999
        with self.assertRaises(ImmutableAfterConfirmError):
            self.st.save()
        self.line.override_amount = 1
        with self.assertRaises(ImmutableAfterConfirmError):
            self.line.save()
        with self.assertRaises(ImmutableAfterConfirmError):
            PayrollLine(statement=self.st, name="追加", kind="earning", auto_amount=5).save()
        with self.assertRaises(ImmutableAfterConfirmError):
            PayrollStatement.objects.get(pk=self.st.pk).delete()

    def test_db_level_guard_blocks_bulk_update(self):
        # ORM の save() を通らない一括更新も DB トリガーで止まる
        for op in (
            lambda: PayrollStatement.objects.filter(pk=self.st.pk).update(net_amount=1),
            lambda: PayrollLine.objects.filter(pk=self.line.pk).update(auto_amount=1),
            lambda: PayrollLine.objects.filter(pk=self.line.pk).delete(),
            lambda: PayrollStatement.objects.filter(pk=self.st.pk).delete(),
        ):
            with self.assertRaises(Exception), transaction.atomic():
                op()
        self.st.refresh_from_db()
        self.assertEqual(self.st.net_amount, 1000)

    def test_review_flag_can_still_be_set(self):
        # 確定後に経費が追加された場合などの「要確認」通知は立てられる（金額は変わらない）
        PayrollStatement.objects.filter(pk=self.st.pk).update(needs_review=True, review_note="経費の追加あり")
        self.st.refresh_from_db()
        self.assertTrue(self.st.needs_review)
        self.assertEqual(self.st.net_amount, 1000)
