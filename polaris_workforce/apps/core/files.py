"""Protected downloads of uploaded files (worker photos, payment proofs).

Nothing under ``MEDIA_ROOT`` is served directly. A file is reached through a URL of the
object it belongs to, and only by someone who may see that object's site:

- Worker photo: ``/files/workers/<id>/photo/`` (office, session) and
  ``/api/v1/files/workers/<id>/photo/`` (device token or session). Allowed when the
  worker has an assignment (past, current or future) on a site the user sees.
- Payment proof: ``/files/proofs/<payment id>/`` and ``/api/v1/files/proofs/<payment id>/``.
  Allowed when the payment's site is visible (``scoped_sites`` / ``can_view_site``).
- Check-in photo: ``/checkin/photos/<check-in id>/`` and
  ``/api/v1/files/checkins/<check-in id>/photo/`` (rules in ``apps.checkin.files``).
- Any file by stored name: ``/files/media/<name>``, which is what ``FieldFile.url`` gives
  (Django admin links). Admins only (403 for everyone else); a company admin only gets
  files of their own company's workers, payments and advances (404 otherwise).

An object out of scope answers 404, like an object without a file, so ids cannot be
probed. The view checks access, then ``file_response`` hands the file over: Django streams
it, or with ``PROTECTED_MEDIA_MODE=nginx|apache`` the web server sends it
(``X-Accel-Redirect`` / ``X-Sendfile``), see ``docs/security-review.md``.
"""

import posixpath
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.db.models import Exists, OuterRef
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotModified
from django.urls import reverse
from django.utils.http import content_disposition_header

from apps.accounts.permissions import Role, company_workers, has_role, is_admin, scoped_sites
from apps.checkin.models import CheckIn
from apps.core.tenancy import company_id_of, is_platform_admin
from apps.payroll.models import Advance, Payment
from apps.workers.models import Worker
from apps.worksites.models import SiteAssignment

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
INLINE_TYPES = {"image/jpeg", "image/png", "image/webp"}  # shown in <img>; the rest downloads


# --- who may see what -------------------------------------------------------------------


def visible_workers(user):
    """Workers whose photo ``user`` may see: the whole company for admins and accountants,
    else the workers with an assignment (past, current or future) on a site the user sees."""
    workers = company_workers(user)
    if has_role(user, Role.ADMIN, Role.ACCOUNTANT):
        return workers
    on_my_sites = SiteAssignment.objects.filter(worker=OuterRef("pk"), site__in=scoped_sites(user))
    return workers.filter(Exists(on_my_sites))


def visible_payments(user):
    """Payments made on the sites ``user`` may see."""
    return Payment.objects.filter(pay_line__period__site__in=scoped_sites(user))


def worker_photo_or_404(user, worker_id) -> str:
    """The stored name of the worker's photo, if ``user`` may see the worker."""
    worker = visible_workers(user).filter(pk=worker_id).first()
    if worker is None or not worker.photo:
        raise Http404
    return worker.photo.name


def payment_proof_or_404(user, payment_id) -> str:
    """The stored name of the payment's proof file, if ``user`` may see its site."""
    payment = visible_payments(user).filter(pk=payment_id).first()
    if payment is None or not payment.proof_file:
        raise Http404
    return payment.proof_file.name


def media_name_or_404(user, name: str) -> str:
    """Any stored file by name, for admins only (``FieldFile.url`` in Django admin).

    Platform admins get any file; a company admin only the files of their company.
    """
    if not is_admin(user):
        raise Http404
    name = posixpath.normpath(name)
    if name in {".", ".."} or name.startswith(("/", "../")) or "\\" in name:
        raise Http404
    if not is_platform_admin(user) and not _company_owns_file(company_id_of(user), name):
        raise Http404
    return name


def _company_owns_file(company_id, name: str) -> bool:
    """True if ``name`` is the file of one of the company's records. Every model with a
    file field must be listed here."""
    if company_id is None:
        return False
    return (
        Worker.objects.filter(company_id=company_id, photo=name).exists()
        or Payment.objects.filter(
            pay_line__period__site__company_id=company_id, proof_file=name
        ).exists()
        or Advance.objects.filter(worker__company_id=company_id, proof_file=name).exists()
        or CheckIn.objects.filter(site__company_id=company_id, photo=name).exists()
    )


def _exists(name: str) -> bool:
    try:
        return default_storage.exists(name)
    except SuspiciousFileOperation as exc:  # a path outside MEDIA_ROOT
        raise Http404 from exc


# --- URLs ------------------------------------------------------------------------------


def worker_photo_url(worker) -> str | None:
    """Office URL of the worker's photo (``None`` without a photo)."""
    return reverse("core:worker_photo", args=[worker.pk]) if worker.photo else None


def payment_proof_url(payment) -> str | None:
    """Office URL of the payment's proof file (``None`` without a file)."""
    return reverse("core:payment_proof", args=[payment.pk]) if payment.proof_file else None


def api_worker_photo_url(request, worker) -> str | None:
    """Absolute API URL of the worker's photo; the phone sends its device token."""
    if not worker.photo:
        return None
    return request.build_absolute_uri(reverse("api:file-worker-photo", args=[worker.pk]))


def api_payment_proof_url(request, payment) -> str | None:
    """Absolute API URL of the payment's proof file; needs the device token."""
    if not payment.proof_file:
        return None
    return request.build_absolute_uri(reverse("api:file-payment-proof", args=[payment.pk]))


# --- handing the file over ---------------------------------------------------------------


def file_response(request, name: str) -> HttpResponse:
    """The response for a stored file the caller may see (404 if it is gone from the disk).

    Files never change under a name (names are random), so the name is the ETag and a
    repeat download answers ``304`` — cheap for the phone on a slow network.
    """
    if not _exists(name):
        raise Http404
    basename = posixpath.basename(name)
    extension = posixpath.splitext(name)[1].lower()
    content_type = CONTENT_TYPES.get(extension, "application/octet-stream")
    etag = f'"{basename}"'
    if etag in [tag.strip() for tag in request.headers.get("If-None-Match", "").split(",")]:
        response = HttpResponseNotModified()
    elif settings.PROTECTED_MEDIA_MODE == "nginx":
        response = HttpResponse(content_type=content_type)
        internal = settings.PROTECTED_MEDIA_INTERNAL_URL.rstrip("/") + "/"
        response["X-Accel-Redirect"] = internal + quote(name)
    elif settings.PROTECTED_MEDIA_MODE == "apache":
        response = HttpResponse(content_type=content_type)
        response["X-Sendfile"] = default_storage.path(name)
    else:
        response = FileResponse(default_storage.open(name, "rb"), content_type=content_type)
    if response.status_code == 200:
        attachment = content_type not in INLINE_TYPES
        response["Content-Disposition"] = content_disposition_header(attachment, basename)
    response["ETag"] = etag
    # Only the signed-in viewer's browser or phone may keep a copy; check before reusing.
    response["Cache-Control"] = "private, no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response
