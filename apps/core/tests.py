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
