"""監査ログの書き込み口。重要な操作は必ずここを通して記録する。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.db import models

from .middleware import get_current_request
from .models import AuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, models.Model):
        return {"id": value.pk, "repr": str(value)}
    return value


def _client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log(
    action: str,
    *,
    target: models.Model | None = None,
    target_type: str = "",
    target_id: str | int = "",
    target_repr: str = "",
    before: Any = None,
    after: Any = None,
    reason: str = "",
    user=None,
    request=None,
) -> AuditLog:
    request = request or get_current_request()
    if user is None and request is not None and getattr(request, "user", None) is not None:
        user = request.user if request.user.is_authenticated else None
    if target is not None:
        target_type = target_type or target._meta.label
        target_id = target_id or target.pk
        target_repr = target_repr or str(target)
    return AuditLog.objects.create(
        user=user,
        username=getattr(user, "username", "") or "",
        action=action,
        target_type=target_type,
        target_id=str(target_id or ""),
        target_repr=(target_repr or "")[:255],
        before=_jsonable(before),
        after=_jsonable(after),
        reason=reason or "",
        ip=_client_ip(request),
    )
