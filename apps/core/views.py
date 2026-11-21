from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.accounts import stepup


@login_required
def dashboard(request):
    # 給与・経費の金額は、それぞれ追加認証済みの場合のみ表示する（STEP 3 以降で中身を実装）
    return render(
        request,
        "core/dashboard.html",
        {
            "payroll_verified": stepup.is_verified(request, "payroll", touch=False),
            "expenses_verified": stepup.is_verified(request, "expenses", touch=False),
        },
    )
