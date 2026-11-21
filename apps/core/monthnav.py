from datetime import date

from django.utils import timezone

from .months import month_start, next_month, parse_month, prev_month


def month_from_request(request, param: str = "month") -> date:
    raw = request.GET.get(param) or request.POST.get(param)
    try:
        return parse_month(raw) if raw else month_start(timezone.localdate())
    except (ValueError, TypeError):
        return month_start(timezone.localdate())


def month_context(month: date) -> dict:
    return {
        "month": month,
        "month_param": f"{month:%Y-%m}",
        "prev_param": f"{prev_month(month):%Y-%m}",
        "next_param": f"{next_month(month):%Y-%m}",
    }
