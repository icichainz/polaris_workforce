"""Office views for check-in: overview, a site's day, badges (list, print, issue, revoke),
kiosk phones and geofences.

Admins see every site of their company, site managers their own sites (CLAUDE.md rule
8): every page starts from ``scoped_sites`` / ``scoped_workers``; an object out of scope
answers 403 (sites) or 404 (workers, devices, photos). Writes go through
``apps/checkin/services.py``, which writes the audit entries.
"""

import datetime

import segno
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ForemanDevice, User
from apps.accounts.permissions import (
    Role,
    can_manage_site,
    company_users,
    get_site_or_403,
    role_required,
    scoped_sites,
)
from apps.core import dates
from apps.core import files as core_files
from apps.core.models import CompanySettings
from apps.workers.services import scoped_workers
from apps.worksites.models import SiteAssignment

from . import files, selectors, services
from .forms import BadgeFilterForm, DayPickForm, GeofenceForm, KioskForm, RevokeForm
from .models import Badge, CheckIn, KioskDevice, SiteGeofence

MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)
BADGES_PER_PAGE = 10  # 2 columns × 5 rows on A4


def _flag_label(flag: str) -> str:
    return {
        selectors.UNKNOWN_BADGE: _("Unknown badge"),
        selectors.REVOKED_BADGE: _("Revoked badge"),
        selectors.NOT_ON_CREW: _("Not on crew"),
        selectors.OUTSIDE_GEOFENCE: _("Outside geofence"),
        selectors.DUPLICATE: _("Duplicate (< 5 min)"),
    }[flag]


def _managed_sites(user):
    return scoped_sites(user).order_by("name")


def _workers(user):
    return scoped_workers(user, dates.company_today()).select_related("trade")


def _back(request, default: str):
    """Redirect to the page the form came from (same site only), else ``default``."""
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect(default)


# --- overview and a site's day -------------------------------------------------------------


@role_required(*MANAGERS)
def home(request):
    """Today's check-ins per site. With ``?site=&date=`` (the day picker): that day."""
    if "site" in request.GET:
        pick = DayPickForm(request.GET, sites=_managed_sites(request.user))
        if pick.is_valid():
            return redirect(
                "checkin:site_day", pk=pick.cleaned_data["site"].pk, date=pick.cleaned_data["date"]
            )
    company = CompanySettings.load()
    today = dates.company_today(company)
    sites = _managed_sites(request.user).annotate(
        checkins_today=Count("checkins", filter=Q(checkins__date=today)),
        workers_today=Count("checkins__worker", filter=Q(checkins__date=today), distinct=True),
    )
    geofenced = set(SiteGeofence.objects.filter(site__in=sites).values_list("site_id", flat=True))
    kiosks = dict(
        KioskDevice.objects.filter(site__in=sites)
        .values("site_id")
        .annotate(n=Count("pk"))
        .values_list("site_id", "n")
    )
    rows = [
        {
            "site": site,
            "checkins": site.checkins_today,
            "workers": site.workers_today,
            "geofence": site.pk in geofenced,
            "kiosks": kiosks.get(site.pk, 0),
        }
        for site in sites
    ]
    return render(
        request,
        "checkin/home.html",
        {
            "rows": rows,
            "today": today,
            "mode": company.get_attendance_mode_display(),
            "mode_value": company.attendance_mode,
        },
    )


@role_required(*MANAGERS)
def site_day(request, pk, date):
    site = get_site_or_403(request.user, pk)
    tz = dates.company_tz(CompanySettings.load(site.company_id))
    checkins = selectors.day_checkins(site, date)
    duplicates = selectors.duplicate_ids(checkins)
    rows = []
    for checkin in checkins:
        flags = selectors.flags_of(checkin, duplicates)
        rows.append(
            {
                "checkin": checkin,
                "time": checkin.at.astimezone(tz).strftime("%H:%M"),
                "flags": [{"code": f, "label": _flag_label(f)} for f in flags],
                "photo_url": files.checkin_photo_url(checkin),
            }
        )
    result = selectors.day_suggestions(site, date)
    names = {
        w.pk: w
        for w in _workers(request.user).filter(pk__in=[s.worker_id for s in result.suggestions])
    }
    suggestions = [{"s": s, "worker": names.get(s.worker_id)} for s in result.suggestions]
    form = DayPickForm(initial={"site": site, "date": date}, sites=_managed_sites(request.user))
    return render(
        request,
        "checkin/site_day.html",
        {
            "site": site,
            "date": date,
            "previous": date - datetime.timedelta(days=1),
            "next": date + datetime.timedelta(days=1),
            "rows": rows,
            "flagged": sum(1 for row in rows if row["flags"]),
            "suggestions": suggestions,
            "presence_shown": any(s.presence for s in result.suggestions),
            "result": result,
            "form": form,
            "nav_active": "checkin:home",
        },
    )


@role_required(*MANAGERS)
def photo(request, pk):
    return core_files.file_response(request, files.checkin_photo_or_404(request.user, pk))


# --- badges --------------------------------------------------------------------------------


@role_required(*MANAGERS)
def badges(request):
    sites = _managed_sites(request.user)
    form = BadgeFilterForm(request.GET or None, sites=sites)
    workers = _workers(request.user).filter(is_active=True)
    site = None
    if form.is_valid():
        workers = workers.search(form.cleaned_data["q"])
        site = form.cleaned_data["site"]
        if site is not None:
            on_site = SiteAssignment.objects.filter(site=site).values("worker_id")
            workers = workers.filter(pk__in=on_site)
    workers = list(workers.order_by("last_name", "first_name", "pk")[:500])
    active = {
        badge.worker_id: badge
        for badge in Badge.objects.filter(worker__in=workers, revoked_at__isnull=True)
    }
    rows = [{"worker": w, "badge": active.get(w.pk)} for w in workers]
    print_url = reverse("checkin:badge_sheet")
    if site is not None:
        print_url += f"?site={site.pk}"
    return render(
        request,
        "checkin/badges.html",
        {
            "form": form,
            "rows": rows,
            "site": site,
            "missing": sum(1 for row in rows if row["badge"] is None),
            "print_url": print_url,
            "revoke_form": RevokeForm(),
        },
    )


QR_BORDER = 4  # quiet zone, in modules


def qr_path(payload: str) -> tuple[int, str]:
    """``(size, path data)`` of the badge's QR code (segno, error level M), for an SVG
    drawn in the template: one ``M x,y h n v1 h-n z`` rectangle per run of dark modules.
    Only digits and path letters: plain autoescaped text in the template."""
    qr = segno.make_qr(payload, error="m")
    parts = []
    rows = list(qr.matrix_iter(border=QR_BORDER))
    for y, row in enumerate(rows):
        x, row = 0, list(row)
        while x < len(row):
            if row[x]:
                start = x
                while x < len(row) and row[x]:
                    x += 1
                parts.append(f"M{start},{y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return len(rows), "".join(parts)


@role_required(*MANAGERS)
def badge_sheet(request):
    """A4 print page: 10 badges per sheet (QR as inline SVG, name, number, trade)."""
    workers = _workers(request.user).filter(is_active=True)
    site = None
    site_id = request.GET.get("site", "")
    if site_id.isdigit():
        site = get_site_or_403(request.user, int(site_id))
        workers = workers.filter(
            pk__in=SiteAssignment.objects.filter(site=site).values("worker_id")
        )
    chosen = [int(v) for v in request.GET.getlist("worker") if v.isdigit()]
    if chosen:
        workers = workers.filter(pk__in=chosen)
    badges_qs = (
        Badge.objects.filter(worker__in=workers, revoked_at__isnull=True)
        .select_related("worker__trade")
        .order_by("worker__last_name", "worker__first_name", "worker_id")
    )
    company = CompanySettings.load()
    cards = [
        {
            "badge": badge,
            "worker": badge.worker,
            "qr": qr_path(badge.payload),
            "photo_url": core_files.worker_photo_url(badge.worker),
        }
        for badge in badges_qs
    ]
    pages = [cards[i : i + BADGES_PER_PAGE] for i in range(0, len(cards), BADGES_PER_PAGE)]
    return render(
        request,
        "checkin/badge_sheet.html",
        {
            "pages": pages,
            "count": len(cards),
            "site": site,
            "company_name": company.company_name or str(company.company),
        },
    )


def _worker_or_404(user, pk):
    worker = _workers(user).filter(pk=pk).first()
    if worker is None:
        raise Http404
    return worker


@require_POST
@role_required(*MANAGERS)
def badge_issue(request, pk):
    worker = _worker_or_404(request.user, pk)
    had = services.active_badge(worker) is not None
    services.issue_badge(worker, request.user)
    if had:
        messages.success(
            request,
            _("New badge for %(name)s; the old one no longer works.") % {"name": worker.full_name},
        )
    else:
        messages.success(request, _("Badge issued for %(name)s.") % {"name": worker.full_name})
    return _back(request, reverse("checkin:badges"))


@require_POST
@role_required(*MANAGERS)
def badge_revoke(request, pk):
    worker = _worker_or_404(request.user, pk)
    badge = services.active_badge(worker)
    form = RevokeForm(request.POST)
    if badge is None:
        messages.warning(request, _("%(name)s has no active badge.") % {"name": worker.full_name})
    elif form.is_valid():
        services.revoke_badge(badge, request.user, form.cleaned_data["reason"])
        messages.success(request, _("Badge of %(name)s revoked.") % {"name": worker.full_name})
    return _back(request, reverse("checkin:badges"))


@require_POST
@role_required(*MANAGERS)
def badges_issue_missing(request, pk):
    site = get_site_or_403(request.user, pk)
    workers = _workers(request.user).filter(
        is_active=True, pk__in=SiteAssignment.objects.filter(site=site).values("worker_id")
    )
    issued = services.issue_missing_badges(list(workers), request.user)
    messages.success(
        request, _("%(count)s badges issued for %(site)s.") % {"count": len(issued), "site": site}
    )
    return redirect(reverse("checkin:badges") + f"?site={site.pk}")


# --- kiosk phones ------------------------------------------------------------------------


def _visible_devices(user):
    """Signed-in phones of the foremen of the user's sites (company first)."""
    foremen = company_users(user).filter(
        role=User.Role.FOREMAN, foreman_sites__in=scoped_sites(user)
    )
    return ForemanDevice.objects.filter(user__in=foremen, revoked_at__isnull=True).distinct()


@role_required(*MANAGERS)
def kiosks(request):
    devices = (
        _visible_devices(request.user)
        .select_related("user", "kiosk__site")
        .order_by("user__last_name", "user__first_name", "-last_seen_at")
    )
    rows = []
    for device in devices:
        kiosk = getattr(device, "kiosk", None)
        candidates = services.kiosk_candidate_sites(device, request.user)
        rows.append(
            {
                "device": device,
                "kiosk": kiosk,
                "form": KioskForm(
                    sites=scoped_sites(request.user).filter(pk__in=[s.pk for s in candidates]),
                    initial={"site": kiosk.site if kiosk else None},
                    prefix=f"d{device.pk}",
                )
                if candidates
                else None,
                "can_end": kiosk is not None and can_manage_site(request.user, kiosk.site),
            }
        )
    return render(request, "checkin/kiosks.html", {"rows": rows})


def _device_or_404(user, pk) -> ForemanDevice:
    device = _visible_devices(user).select_related("user").filter(pk=pk).first()
    if device is None:
        raise Http404
    return device


@require_POST
@role_required(*MANAGERS)
def kiosk_set(request, pk):
    device = _device_or_404(request.user, pk)
    candidates = services.kiosk_candidate_sites(device, request.user)
    form = KioskForm(
        request.POST,
        sites=scoped_sites(request.user).filter(pk__in=[s.pk for s in candidates]),
        prefix=f"d{device.pk}",
    )
    if not form.is_valid():
        messages.error(request, _("Pick a site this foreman works on."))
        return redirect("checkin:kiosks")
    try:
        services.enable_kiosk(device, form.cleaned_data["site"], request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(str(m) for msgs in exc.detail.values() for m in msgs))
        return redirect("checkin:kiosks")
    messages.success(
        request,
        _("%(device)s is now the kiosk of %(site)s.")
        % {"device": device.name or device.device_id, "site": form.cleaned_data["site"]},
    )
    return redirect("checkin:kiosks")


@require_POST
@role_required(*MANAGERS)
def kiosk_end(request, pk):
    device = _device_or_404(request.user, pk)
    kiosk = KioskDevice.objects.filter(device=device).select_related("site", "device").first()
    if kiosk is None:
        raise Http404
    if not can_manage_site(request.user, kiosk.site):
        raise PermissionDenied
    services.disable_kiosk(kiosk, request.user)
    messages.success(
        request,
        _("%(device)s is an ordinary foreman phone again.")
        % {"device": device.name or device.device_id},
    )
    return redirect("checkin:kiosks")


# --- geofences -----------------------------------------------------------------------------


@role_required(*MANAGERS)
def geofences(request):
    sites = _managed_sites(request.user).select_related("geofence")
    rows = [
        {
            "site": site,
            "geofence": getattr(site, "geofence", None),
            "can_edit": can_manage_site(request.user, site),
        }
        for site in sites
    ]
    return render(request, "checkin/geofences.html", {"rows": rows})


@role_required(*MANAGERS)
def geofence(request, pk):
    site = get_site_or_403(request.user, pk)
    if not can_manage_site(request.user, site):
        raise PermissionDenied
    current = SiteGeofence.objects.filter(site=site).first()
    if request.method == "POST":
        if request.POST.get("action") == "clear":
            services.clear_geofence(site, request.user)
            messages.success(request, _("Geofence removed for %(site)s.") % {"site": site})
            return redirect("checkin:geofences")
        form = GeofenceForm(request.POST)
        if form.is_valid():
            services.set_geofence(site, actor=request.user, **form.cleaned_data)
            messages.success(request, _("Geofence saved for %(site)s.") % {"site": site})
            return redirect("checkin:geofences")
    else:
        initial = (
            {
                "latitude": current.latitude,
                "longitude": current.longitude,
                "radius_m": current.radius_m,
            }
            if current
            else None
        )
        form = GeofenceForm(initial=initial)
    recent = CheckIn.objects.filter(site=site, latitude__isnull=False).order_by("-at")[:5]
    return render(
        request,
        "checkin/geofence.html",
        {
            "site": site,
            "form": form,
            "current": current,
            "recent": recent,
            "nav_active": "checkin:home",
        },
    )
