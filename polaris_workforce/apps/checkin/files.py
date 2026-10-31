"""Check-in photos: who may see them, and their protected URLs.

Same rules as the other uploads (``apps.core.files``): never served from ``MEDIA_ROOT``;
reached through the check-in's URL, only by someone who may see the check-in's site
(``scoped_sites``), else 404 — like a check-in without a photo. ``file_response`` hands
the file over (ETag, ``nosniff``, web-server offload).
"""

from django.http import Http404
from django.urls import reverse

from apps.accounts.permissions import scoped_sites

from .models import CheckIn


def visible_checkins(user):
    """Check-ins on the sites ``user`` may see (always within their company)."""
    return CheckIn.objects.filter(site__in=scoped_sites(user))


def checkin_photo_or_404(user, checkin_id) -> str:
    """The stored name of the check-in's photo, if ``user`` may see its site."""
    checkin = visible_checkins(user).filter(pk=checkin_id).first()
    if checkin is None or not checkin.photo:
        raise Http404
    return checkin.photo.name


def checkin_photo_url(checkin) -> str | None:
    """Office URL of the check-in's photo (``None`` without a photo)."""
    return reverse("checkin:photo", args=[checkin.pk]) if checkin.photo else None


def api_checkin_photo_url(request, checkin) -> str | None:
    """Absolute API URL of the check-in's photo; the phone sends its device token."""
    if not checkin.photo:
        return None
    return request.build_absolute_uri(reverse("api:checkin:file-photo", args=[checkin.pk]))
