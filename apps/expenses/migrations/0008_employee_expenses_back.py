"""以前は正社員の経費を全部「対象外」にしていた。交通費・経費は正社員の給与の合計に入れるので、確認待ちに戻す。"""

from django.db import migrations
from django.db.models import Q


def forward(apps, schema_editor):
    Rec = apps.get_model("expenses", "ExpenseRecord")
    qs = Rec.objects.filter(status="excluded", note="正社員のため給与計算（明細）の対象外です。").exclude(
        Q(category__code="gasoline") | Q(category__isnull=True, auto_category__code="gasoline"))
    qs.update(status="unchecked", note="")


class Migration(migrations.Migration):
    dependencies = [("expenses", "0007_processing_started_at")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
