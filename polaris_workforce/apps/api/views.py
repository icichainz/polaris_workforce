from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    """Lets the Flutter app check that the server is reachable before syncing."""
    return JsonResponse({"status": "ok"})
