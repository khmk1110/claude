from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.accounts.decorators import stepup_required


@login_required
@stepup_required("payroll")
def index(request):
    return render(request, "payroll/index.html")
