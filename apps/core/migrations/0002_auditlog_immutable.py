"""監査ログを DB レベルで変更・削除不可にする（PostgreSQL のみ）。"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION core_auditlog_block_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '操作履歴は変更・削除できません';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS core_auditlog_no_update_delete ON core_auditlog;
CREATE TRIGGER core_auditlog_no_update_delete
    BEFORE UPDATE OR DELETE ON core_auditlog
    FOR EACH ROW EXECUTE FUNCTION core_auditlog_block_change();
"""

REVERSE = """
DROP TRIGGER IF EXISTS core_auditlog_no_update_delete ON core_auditlog;
DROP FUNCTION IF EXISTS core_auditlog_block_change();
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD)


def reverse(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(forward, reverse)]
