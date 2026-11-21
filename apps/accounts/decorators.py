from functools import wraps
from urllib.parse import urlencode

from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from . import stepup


def wants_json(request) -> bool:
    return (
        request.path.startswith("/api/")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def stepup_denied_response(request, scope: str):
    """未認証時の応答。画面ならパスワード入力へ、API・ダウンロードは 403。"""
    if not request.user.is_authenticated:
        if wants_json(request):
            return JsonResponse({"detail": "ログインが必要です。"}, status=401)
        return redirect_to_login(request.get_full_path())
    if wants_json(request) or request.method not in ("GET", "HEAD"):
        return JsonResponse(
            {"detail": "追加パスワードの認証が必要です。", "scope": scope}, status=403
        )
    url = reverse("stepup", kwargs={"scope": scope})
    return redirect(f"{url}?{urlencode({'next': request.get_full_path()})}")


def stepup_required(scope: str):
    """ビュー用デコレーター。ミドルウェアと二重で検証する。"""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not stepup.is_verified(request, scope):
                return stepup_denied_response(request, scope)
            return view(request, *args, **kwargs)

        wrapped.stepup_scope = scope
        return wrapped

    return decorator
