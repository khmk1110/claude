"""給与・経費の追加パスワード認証（サーバー側）。

- 認証状態はサーバー側セッションにのみ保持する（Cookie に結果を書かない）。
- 一定時間操作がない／認証から一定時間経過／ログアウト時は無効になる。
- 連続して間違えると一定時間ロックする。
- 給与と経費は独立している（片方の認証でもう片方は開けない）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core import audit
from apps.core.audit import _client_ip

from .models import StepUpAttempt, StepUpPassword, StepUpPasswordHistory, StepUpScope

MIN_LENGTH = 4


def _session_key(scope: str) -> str:
    return f"stepup:{scope}"


def _check_scope(scope: str) -> None:
    if scope not in StepUpScope.values:
        raise ValueError(f"unknown step-up scope: {scope}")


@dataclass
class VerifyResult:
    ok: bool
    locked: bool = False
    locked_until: timezone.datetime | None = None
    not_configured: bool = False
    message: str = ""


def locked_until(user, scope: str):
    """ロック中ならロック解除時刻、そうでなければ None。"""
    window_start = timezone.now() - timedelta(seconds=settings.STEPUP_LOCK_SECONDS)
    recent = list(
        StepUpAttempt.objects.filter(user=user, scope=scope, at__gte=window_start)
        .order_by("-at")
        .values_list("success", "at")[: settings.STEPUP_MAX_FAILURES]
    )
    if len(recent) < settings.STEPUP_MAX_FAILURES or any(ok for ok, _ in recent):
        return None
    last_failure_at = recent[0][1]
    return last_failure_at + timedelta(seconds=settings.STEPUP_LOCK_SECONDS)


def verify(request, scope: str, raw_password: str) -> VerifyResult:
    _check_scope(scope)
    user = request.user
    until = locked_until(user, scope)
    if until is not None:
        audit.log("stepup.locked", target_type="stepup", target_id=scope, request=request)
        return VerifyResult(
            ok=False,
            locked=True,
            locked_until=until,
            message=f"入力の誤りが続いたため、{timezone.localtime(until):%H:%M} まで入力できません。",
        )

    stored = StepUpPassword.objects.filter(scope=scope).first()
    if stored is None:
        return VerifyResult(
            ok=False,
            not_configured=True,
            message="この機能のパスワードがまだ設定されていません。代表者に設定を依頼してください。",
        )

    ok = bool(raw_password) and check_password(raw_password, stored.password_hash)
    StepUpAttempt.objects.create(user=user, scope=scope, success=ok, ip=_client_ip(request))
    audit.log(
        "stepup.success" if ok else "stepup.failure",
        target_type="stepup",
        target_id=scope,
        target_repr=StepUpScope(scope).label,
        request=request,
    )
    if not ok:
        return VerifyResult(ok=False, message="パスワードが正しくありません。")

    now = time.time()
    request.session.cycle_key()  # セッション固定化攻撃の対策
    request.session[_session_key(scope)] = {"verified_at": now, "last_seen": now}
    return VerifyResult(ok=True)


def is_verified(request, scope: str, *, touch: bool = True) -> bool:
    """追加認証が有効か。有効なら最終操作時刻を更新する（touch=True）。"""
    _check_scope(scope)
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    data = request.session.get(_session_key(scope))
    if not data:
        return False
    now = time.time()
    if now - data.get("last_seen", 0) > settings.STEPUP_IDLE_SECONDS or now - data.get(
        "verified_at", 0
    ) > settings.STEPUP_MAX_AGE_SECONDS:
        request.session.pop(_session_key(scope), None)
        return False
    if touch:
        data["last_seen"] = now
        request.session[_session_key(scope)] = data
    return True


def revoke(request, scope: str | None = None) -> None:
    for s in [scope] if scope else StepUpScope.values:
        request.session.pop(_session_key(s), None)


def set_password(scope: str, raw_password: str, *, by=None, note: str = "") -> None:
    _check_scope(scope)
    if not raw_password or len(raw_password) < MIN_LENGTH:
        raise ValidationError(f"パスワードは{MIN_LENGTH}文字以上にしてください。")
    obj, created = StepUpPassword.objects.update_or_create(
        scope=scope, defaults={"password_hash": make_password(raw_password), "updated_by": by}
    )
    StepUpPasswordHistory.objects.create(scope=scope, changed_by=by, note=note)
    audit.log(
        "stepup.password_set" if created else "stepup.password_changed",
        target_type="stepup",
        target_id=scope,
        target_repr=StepUpScope(scope).label,
        user=by,
        reason=note,
    )
