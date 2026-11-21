from django.conf import settings


def company(request):
    from apps.accounts import stepup

    verified = {}
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        verified = {s: stepup.is_verified(request, s, touch=False) for s in ("payroll", "expenses")}
    return {"COMPANY_NAME": settings.COMPANY_NAME, "STEPUP_VERIFIED": verified}
