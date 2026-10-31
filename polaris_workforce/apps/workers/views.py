"""Office views for workers (placeholder until Wave 2, agent B)."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def worker_list(request):
    return render(request, "workers/list.html")
