"""What a kiosk phone may call (Wave 4 integration).

A kiosk is a foreman's phone switched to kiosk mode for one site (``KioskDevice``); it
is left on the site for workers to scan their badges. Its device token is the foreman's,
so without a limit anyone at the kiosk could submit roll calls, correct records or pay
people. The server therefore answers ``403 permission_denied`` to every API endpoint a
kiosk phone does not need.

Enforced once, for every endpoint, by ``apps.api.authentication.DeviceTokenAuthentication``
(``enforce_kiosk_scope`` right after the token is recognised): only requests made with a
device token are concerned; an office session is never a kiosk. The kiosk's own site is
checked by the check-in views (``apps/checkin/api/views.py``).
"""

from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions

from .selectors import kiosk_of

# View names (``request.resolver_match.view_name``) a kiosk phone may call, any method.
KIOSK_ALLOWED = frozenset(
    {
        "api:health",
        "api:schema",
        "api:not-found",
        "api:accounts:me",
        "api:accounts:auth-logout",  # signing the phone out is always allowed
        "api:file-worker-photo",  # the worker's photo on the scan screen
    }
)
# Every check-in endpoint: check-ins, this device, setup, the site's check-ins,
# suggestions and check-in photos.
KIOSK_ALLOWED_NAMESPACES = ("api:checkin:",)
# Read only: the names behind the badge hashes of ``checkin-setup`` (offline lookup).
KIOSK_READ_ONLY = frozenset({"api:worksites:crew"})
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

KIOSK_REFUSED = _(
    "This phone is in kiosk mode: it can only record check-ins. Ask the office to end "
    "kiosk mode to use the other screens."
)


def kiosk_may_call(view_name: str, method: str) -> bool:
    if view_name in KIOSK_ALLOWED or view_name.startswith(KIOSK_ALLOWED_NAMESPACES):
        return True
    return view_name in KIOSK_READ_ONLY and method.upper() in SAFE_METHODS


def enforce_kiosk_scope(request, device) -> None:
    """Raise ``PermissionDenied`` when ``device`` is a kiosk and the endpoint is not one a
    kiosk may call. ``request`` is the DRF or Django request being authenticated."""
    django_request = getattr(request, "_request", request)
    match = getattr(django_request, "resolver_match", None)
    if match is None:  # not routed (a unit test calling the authenticator directly)
        return
    if kiosk_may_call(match.view_name, django_request.method or ""):
        return
    if kiosk_of(device) is None:
        return
    raise exceptions.PermissionDenied(KIOSK_REFUSED)
