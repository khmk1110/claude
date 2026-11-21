"""確定済み給与を DB レベルで保護する（PostgreSQL のみ）。

- 確定済みのまま金額・スナップショット・対象者・月を変更することを禁止
- 確定済みの給与の削除を禁止
- 確定済みの給与に属する明細行・元データの追加・変更・削除を禁止
アプリ側（models.py）でも同じ検証をしており、これは二重の防御。
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION payroll_statement_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'confirmed' THEN
            RAISE EXCEPTION '確定済みの給与は削除できません';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'confirmed' AND NEW.status = 'confirmed' AND (
        NEW.earnings_total IS DISTINCT FROM OLD.earnings_total OR
        NEW.deductions_total IS DISTINCT FROM OLD.deductions_total OR
        NEW.net_amount IS DISTINCT FROM OLD.net_amount OR
        NEW.snapshot::text IS DISTINCT FROM OLD.snapshot::text OR
        NEW.member_id IS DISTINCT FROM OLD.member_id OR
        NEW.month IS DISTINCT FROM OLD.month
    ) THEN
        RAISE EXCEPTION '確定済みの給与は変更できません';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_statement_guard ON payroll_payrollstatement;
CREATE TRIGGER payroll_statement_guard
    BEFORE UPDATE OR DELETE ON payroll_payrollstatement
    FOR EACH ROW EXECUTE FUNCTION payroll_statement_guard();

CREATE OR REPLACE FUNCTION payroll_child_guard() RETURNS trigger AS $$
DECLARE
    sid bigint;
BEGIN
    IF TG_OP = 'DELETE' THEN sid := OLD.statement_id; ELSE sid := NEW.statement_id; END IF;
    IF EXISTS (SELECT 1 FROM payroll_payrollstatement WHERE id = sid AND status = 'confirmed') THEN
        RAISE EXCEPTION '確定済みの給与は変更できません';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.statement_id <> NEW.statement_id AND EXISTS (
        SELECT 1 FROM payroll_payrollstatement WHERE id = OLD.statement_id AND status = 'confirmed'
    ) THEN
        RAISE EXCEPTION '確定済みの給与は変更できません';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_line_guard ON payroll_payrollline;
CREATE TRIGGER payroll_line_guard
    BEFORE INSERT OR UPDATE OR DELETE ON payroll_payrollline
    FOR EACH ROW EXECUTE FUNCTION payroll_child_guard();

DROP TRIGGER IF EXISTS payroll_input_guard ON payroll_payrollinput;
CREATE TRIGGER payroll_input_guard
    BEFORE INSERT OR UPDATE OR DELETE ON payroll_payrollinput
    FOR EACH ROW EXECUTE FUNCTION payroll_child_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS payroll_input_guard ON payroll_payrollinput;
DROP TRIGGER IF EXISTS payroll_line_guard ON payroll_payrollline;
DROP TRIGGER IF EXISTS payroll_statement_guard ON payroll_payrollstatement;
DROP FUNCTION IF EXISTS payroll_child_guard();
DROP FUNCTION IF EXISTS payroll_statement_guard();
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def reverse(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0001_initial")]
    operations = [migrations.RunPython(forward, reverse)]
