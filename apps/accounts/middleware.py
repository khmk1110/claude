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
