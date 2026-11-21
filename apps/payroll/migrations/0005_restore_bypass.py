"""バックアップからの復元時だけ、確定済み給与・監査ログの保護トリガーを通す。

復元処理（restore_backup）はトランザクション内で SET LOCAL app.restoring = 'on' を設定する。
通常の操作ではこの設定はないため、保護はそのまま有効。
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION payroll_statement_guard() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.restoring', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
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

CREATE OR REPLACE FUNCTION payroll_child_guard() RETURNS trigger AS $$
DECLARE
    sid bigint;
BEGIN
    IF current_setting('app.restoring', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
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

CREATE OR REPLACE FUNCTION core_auditlog_block_change() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.restoring', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '操作履歴は変更・削除できません';
END;
$$ LANGUAGE plpgsql;
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


class Migration(migrations.Migration):
    dependencies = [("payroll", "0004_seed_items"), ("core", "0003_stored_file")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
