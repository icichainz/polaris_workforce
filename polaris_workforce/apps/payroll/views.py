"""Office views for payroll (placeholders until Wave 2, agent D)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def cash_sheets(request):
    return render(request, "payroll/cash_sheets.html")


@login_required
def periods(request):
    return render(request, "payroll/periods.html")


@login_required
def advances(request):
    return render(request, "payroll/advances.html")
