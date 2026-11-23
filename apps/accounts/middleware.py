from django.conf import settings

from . import stepup
from .decorators import stepup_denied_response


class StepUpGateMiddleware:
    """保護対象 URL（給与・経費・その API・管理画面の該当部分）への到達前に追加認証を検証する。"""

    def __init__(self, get_response):
        self.get_response = get_response
        self.prefixes = sorted(
            settings.STEPUP_PROTECTED_PREFIXES.items(), key=lambda kv: len(kv[0]), reverse=True
        )

    def scope_for(self, path: str) -> str | None:
        for prefix, scope in self.prefixes:
            if path.startswith(prefix):
                return scope
        return None

    def __call__(self, request):
        scope = self.scope_for(request.path)
        if scope and not stepup.is_verified(request, scope):
            return stepup_denied_response(request, scope)
        return self.get_response(request)


GUEST_USERNAME = "guest"
# ゲストにしない（ちゃんとログインが必要な）場所。管理画面とログイン画面そのもの
_REAL_LOGIN_PREFIXES = ("/admin/", "/login", "/logout", "/static/", "/healthz")


def get_guest_user():
    """ログインしていない人を「ゲスト」として扱うための利用者（パスワードではログインできない）。"""
    from .models import User

    user, created = User.objects.get_or_create(username=GUEST_USERNAME, defaults={"role": User.Role.STAFF, "is_active": True})
    if created:
        user.set_unusable_password()
        user.save(update_fields=["password"])
    return user


class GuestUserMiddleware:
    """シフト・勤怠・メンバーなどはログインなしで使えるように、ログインしていない人をゲストとして扱う。

    給与・経費・設定は StepUpGateMiddleware が給与のパスワードを求めるので、ゲストでもパスワードなしでは開けない。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(_REAL_LOGIN_PREFIXES):
            request.user = get_guest_user()
            request.is_guest = True
        else:
            request.is_guest = getattr(request.user, "username", "") == GUEST_USERNAME
        return self.get_response(request)
