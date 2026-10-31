"""Office views for sites (placeholder until Wave 2, agent B)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def site_list(request):
    return render(request, "worksites/list.html")
