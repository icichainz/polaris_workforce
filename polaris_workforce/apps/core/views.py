"""Office views for company settings (placeholder until Wave 2, agent E)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def settings_view(request):
    return render(request, "core/settings.html")
