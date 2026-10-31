"""Office views for users and roles (placeholder until Wave 2, agent A)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def users(request):
    return render(request, "accounts/users.html")
