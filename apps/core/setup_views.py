"""代表者向け：初期データの取り込み・バックアップ。

本番（無料プラン）ではサーバーでコマンドを実行できないため、既存ブックの取り込みを画面から行う。
"""

import gzip
import io
import os
import tempfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.management import CommandError, call_command
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts import stepup
from apps.core import audit


def _owner(request):
    if not request.user.can_manage_stepup_passwords:
        raise PermissionDenied


def _run(command, upload, *args, apply=False):
    """アップロードされたファイルで管理コマンドを実行し、出力を返す。"""
    suffix = os.path.splitext(upload.name)[1].lower()
    if suffix not in (".xlsx",):
        raise CommandError("Excel（.xlsx）ファイルを選んでください。")
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
        tmp.flush()
        out, err = io.StringIO(), io.StringIO()
        call_command(command, tmp.name, *args, *(["--apply"] if apply else []), stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()


@login_required
def setup(request):
    _owner(request)
    output, ok = "", None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "issuer":
            from apps.core import audit
            from apps.core.models import SiteSetting

            before, after = {}, {}
            for key in ("issuer_company", "issuer_representative", "issuer_address", "issuer_tel"):
                row, _ = SiteSetting.objects.get_or_create(key=key)
                new = request.POST.get(key, "").strip()[:200]
                if row.value != new:
                    before[key], after[key] = row.value, new
                    row.value = new
                    row.save()
            if after:
                audit.log("core.issuer_update", target_type="core.SiteSetting", target_id="issuer", target_repr="明細書の発行者",
                          before=before, after=after, user=request.user)
            messages.success(request, "明細書の発行者情報を保存しました（確定済みの明細は確定時のままです）。")
            return redirect("setup")
        apply = request.POST.get("apply") == "1"
        upload = request.FILES.get("file")
        pasted = request.POST.get("text", "").strip()
        try:
            if not upload and not (action == "setupfile" and pasted):
                raise CommandError("ファイルを選んでください。")
            if action == "payroll":
                if not stepup.is_verified(request, "payroll"):
                    raise CommandError("単価表は給与のデータです。先に給与・報酬管理を開いて（追加パスワードを入力して）から実行してください。")
                output = _run("import_legacy_payroll_setup", upload, "--effective-from", request.POST.get("effective_from", ""), apply=apply)
            elif action == "setupfile":
                if not stepup.is_verified(request, "payroll"):
                    raise CommandError("チームや給与項目を含むため、先に給与・報酬管理を開いて（追加パスワードを入力して）から実行してください。")
                import json

                from apps.core.setup_file import apply_setup

                try:
                    data = json.loads(pasted if pasted else upload.read().decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    raise CommandError(f"設定ファイルを読み取れません: {e}")
                output = "\n".join(apply_setup(data, user=request.user, dry_run=not apply))
            elif action == "restore":
                from apps.core.management.commands.restore_backup import restore

                with tempfile.NamedTemporaryFile(suffix=".gz") as tmp:
                    for chunk in upload.chunks():
                        tmp.write(chunk)
                    tmp.flush()
                    out = io.StringIO()
                    restore(tmp.name, stdout=out)
                    output = out.getvalue() + "\n復元しました。いったんログアウトし、バックアップ時のユーザーでログインし直してください。"
                apply = True
            elif action == "shift":
                output = _run("import_legacy_shift", upload, *(["--overwrite"] if request.POST.get("overwrite") else []), apply=apply)
            else:
                raise CommandError("不明な操作です。")
            ok = True
            audit.log(f"setup.{action}", target_type="setup", target_repr=upload.name if upload else "貼り付け",
                      after={"apply": apply}, request=request)
            messages.success(request, "取り込みました。" if apply else "確認しました（まだ取り込んでいません）。内容を確認して「取り込む」にチェックして再実行してください。")
        except Exception as e:  # noqa: BLE001 — 失敗内容を画面に表示する（復元は全体がロールバックされる）
            ok = False
            output = (output + "\n" if output else "") + str(e)
            messages.error(request, str(e))
    from apps.members.models import Member
    from apps.payroll.models import PayrollStatement

    can_restore = not Member.objects.exists() and not PayrollStatement.objects.exists()
    from apps.payroll.calc import issuer_info

    return render(request, "core/setup.html", {"output": output, "ok": ok, "can_restore": can_restore, "issuer": issuer_info(),
                                               "pasted": request.POST.get("text", "") if request.method == "POST" else "",
                                               "payroll_verified": stepup.is_verified(request, "payroll", touch=False),
                                               "expenses_verified": True})


@login_required
def backup(request):
    """全データ（資料ファイルを含む）を gzip 圧縮した JSON でダウンロードする。給与・経費の両方の認証が必要。"""
    _owner(request)
    if request.method != "POST":
        return redirect("setup")
    if not stepup.is_verified(request, "payroll"):
        messages.error(request, "バックアップには給与のデータが含まれるため、給与のパスワードを入力してから実行してください。")
        return redirect("setup")
    buf = io.StringIO()
    call_command("dumpdata", "--exclude", "contenttypes", "--exclude", "auth.permission", "--exclude", "sessions",
                 "--exclude", "admin.logentry", "--natural-foreign", stdout=buf)
    data = gzip.compress(buf.getvalue().encode("utf-8"))
    audit.log("setup.backup", target_type="setup", after={"bytes": len(data)}, request=request)
    resp = HttpResponse(data, content_type="application/gzip")
    resp["Content-Disposition"] = f'attachment; filename="kanri-backup-{timezone.localtime():%Y%m%d-%H%M}.json.gz"'
    return resp
