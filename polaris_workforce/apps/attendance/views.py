"""Office views for attendance (placeholder until Wave 2, agent C)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def approvals(request):
    return render(request, "attendance/approvals.html")
