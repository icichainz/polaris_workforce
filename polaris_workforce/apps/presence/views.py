"""Office screens for presence tracking (brief P4): setup per site (zones, gateways and
their tokens, the tracker sync), BLE badges, the live map, a person's timeline, the
worker's presence export and the printed notice for badge holders.

Who sees what (CLAUDE.md rule 8, ``apps.accounts.permissions``):

- Admins and site managers only, as for check-in: presence is about where people are,
  so accountants and foremen get 403 (foremen use the phone).
- Admins: every site of their company. Site managers: the sites they manage. A site out
  of scope answers 403 (``get_site_or_403``); a zone, gateway, badge or worker out of
  scope answers 404, so ids cannot be probed.
- A foreman's timeline: admins only. Off-site travel on a timeline: the tracker returns
  it only to tokens with ``travel`` (company admins, ``services.mint_dashboard_token``).

Writes go through ``apps/presence/services.py``, which audits them and queues the push of
the site's config to the tracker. The browser talks to the tracker itself with a short
dashboard token rendered into the page and refreshed through ``dashboard_token``.
"""

import datetime
import io
import json
import re

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import User
from apps.accounts.permissions import (
    Role,
    can_manage_site,
    company_users,
    get_site_or_403,
    has_role,
    is_admin,
    role_required,
    scoped_sites,
)
from apps.checkin.models import SiteGeofence
from apps.core import dates
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.workers.services import scoped_workers
from apps.worksites.models import SiteAssignment

from . import selectors, services
from .forms import (
    BadgeFilterForm,
    BleBadgeForm,
    DayForm,
    GatewayForm,
    NoticeForm,
    RevokeForm,
    ZoneForm,
)
from .models import BleBadge, Gateway, PresenceEvent, SubjectKind, Zone

MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)
BADGE_ROWS = 500
NAV = "presence:home"


# --- scope ---------------------------------------------------------------------------------


def _sites(user):
    return scoped_sites(user).order_by("name")


def _managed_site(user, pk):
    """A site ``user`` may set up: in scope (else 403) and managed by them (else 403)."""
    site = get_site_or_403(user, pk)
    if not can_manage_site(user, site):
        raise PermissionDenied
    return site


def _zone_or_404(user, pk) -> Zone:
    zone = Zone.objects.filter(site__in=scoped_sites(user), pk=pk).select_related("site").first()
    if zone is None:
        raise Http404
    if not can_manage_site(user, zone.site):
        raise PermissionDenied
    return zone


def _gateway_or_404(user, pk) -> Gateway:
    gateway = (
        Gateway.objects.filter(site__in=scoped_sites(user), pk=pk)
        .select_related("site", "zone")
        .first()
    )
    if gateway is None:
        raise Http404
    if not can_manage_site(user, gateway.site):
        raise PermissionDenied
    return gateway


def _workers(user, today=None):
    return scoped_workers(user, today or dates.company_today())


def _worker_or_404(user, pk):
    worker = _workers(user).select_related("trade").filter(pk=pk).first()
    if worker is None:
        raise Http404
    return worker


def _add_errors(form, exc: ValidationError) -> None:
    """Put a service's ``ValidationError`` on the form (unknown fields: non-field)."""
    if hasattr(exc, "error_dict"):
        for field, errors in exc.error_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, exc)


def _subject_name(user) -> str:
    return user.get_full_name() or user.username


def tracker_public_url(path: str = "") -> str:
    """``TRACKER_PUBLIC_URL`` joined with ``path``; empty without a public URL. The
    tracker may sit under a path behind the proxy (``http://host:8080/tracker``), so the
    path is appended to it, never resolved from the root."""
    base = (settings.TRACKER_PUBLIC_URL or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{path.lstrip('/')}" if path else base


def _page(request, template, context):
    """``render`` with the company time zone: the templates show times in it
    (``{% timezone time_zone %}``), not in the server's."""
    context.setdefault("time_zone", CompanySettings.load().time_zone or "UTC")
    return render(request, template, context)


# --- overview and setup of a site ------------------------------------------------------------


@role_required(*MANAGERS)
def home(request):
    return _page(
        request,
        "presence/home.html",
        {
            "rows": selectors.site_setups(_sites(request.user)),
            "tracker_public": bool(settings.TRACKER_PUBLIC_URL),
            "tracker_push": bool(settings.TRACKER_URL),
        },
    )


@role_required(*MANAGERS)
def site_setup(request, pk):
    site = _managed_site(request.user, pk)
    zones = list(Zone.objects.filter(site=site).order_by("code"))
    gateways = list(
        Gateway.objects.filter(site=site)
        .select_related("zone", "token_issued_by", "revoked_by")
        .order_by("label", "pk")
    )
    return _page(
        request,
        "presence/site_setup.html",
        {
            "site": site,
            "tracked": selectors.is_tracked(site),
            "geofence": SiteGeofence.objects.filter(site=site).first(),
            "zones": zones,
            "gateways": gateways,
            "sync": selectors.sync_status(site),
            "reconciliation": selectors.latest_reconciliation(site),
            "revoke_form": RevokeForm(),
            "nav_active": NAV,
        },
    )


@require_POST
@role_required(*MANAGERS)
def site_push(request, pk):
    site = _managed_site(request.user, pk)
    if not settings.TRACKER_URL:
        messages.warning(request, _("The tracker is not configured on this server."))
    else:
        services.queue_site_push([site.pk])
        messages.success(
            request, _("The settings of %(site)s were sent to the tracker.") % {"site": site}
        )
    return redirect("presence:site_setup", pk=site.pk)


# --- zones ---------------------------------------------------------------------------------------


def _zone_form_view(request, site, zone):
    form = ZoneForm(request.POST or None, instance=zone, site=site)
    if request.method == "POST" and form.is_valid():
        try:
            saved = services.save_zone(form.save(commit=False), actor=request.user)
        except ValidationError as exc:
            _add_errors(form, exc)
        else:
            messages.success(request, _("Zone %(code)s saved.") % {"code": saved.code})
            return redirect("presence:site_setup", pk=site.pk)
    return _page(
        request,
        "presence/zone_form.html",
        {"site": site, "zone": zone if zone.pk else None, "form": form, "nav_active": NAV},
    )


@role_required(*MANAGERS)
def zone_create(request, pk):
    site = _managed_site(request.user, pk)
    return _zone_form_view(request, site, Zone(site=site))


@role_required(*MANAGERS)
def zone_edit(request, pk):
    zone = _zone_or_404(request.user, pk)
    return _zone_form_view(request, zone.site, zone)


@require_POST
@role_required(*MANAGERS)
def zone_delete(request, pk):
    zone = _zone_or_404(request.user, pk)
    site, code = zone.site, zone.code
    services.delete_zone(zone, actor=request.user)
    messages.success(request, _("Zone %(code)s deleted.") % {"code": code})
    return redirect("presence:site_setup", pk=site.pk)


# --- gateways ------------------------------------------------------------------------------------


def _gateway_form_view(request, site, gateway):
    form = GatewayForm(request.POST or None, instance=gateway, site=site)
    if request.method == "POST" and form.is_valid():
        creating = gateway.pk is None
        try:
            saved = services.save_gateway(form.save(commit=False), actor=request.user)
        except ValidationError as exc:
            _add_errors(form, exc)
        else:
            if creating:
                messages.success(
                    request,
                    _("Gateway %(label)s added. Issue its token to connect it.")
                    % {"label": saved.label},
                )
                return redirect("presence:gateway_token", pk=saved.pk)
            messages.success(request, _("Gateway %(label)s saved.") % {"label": saved.label})
            return redirect("presence:site_setup", pk=site.pk)
    return _page(
        request,
        "presence/gateway_form.html",
        {
            "site": site,
            "gateway": gateway if gateway.pk else None,
            "form": form,
            "nav_active": NAV,
        },
    )


@role_required(*MANAGERS)
def gateway_create(request, pk):
    site = _managed_site(request.user, pk)
    return _gateway_form_view(request, site, Gateway(site=site))


@role_required(*MANAGERS)
def gateway_edit(request, pk):
    gateway = _gateway_or_404(request.user, pk)
    return _gateway_form_view(request, gateway.site, gateway)


@never_cache
@role_required(*MANAGERS)
def gateway_token(request, pk):
    """GET: confirm. POST: issue a new token and show it once (it is never stored)."""
    gateway = _gateway_or_404(request.user, pk)
    context = {"gateway": gateway, "site": gateway.site, "nav_active": NAV}
    if request.method == "POST":
        token, expires_at = services.issue_gateway_token(gateway, actor=request.user)
        context.update(
            token=token,
            expires_at=expires_at,
            tracker_url=tracker_public_url(),
            sightings_url=tracker_public_url("v1/gateways/sightings"),
        )
    return _page(request, "presence/gateway_token.html", context)


@require_POST
@role_required(*MANAGERS)
def gateway_revoke(request, pk):
    gateway = _gateway_or_404(request.user, pk)
    form = RevokeForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    if gateway.revoked_at is not None or gateway.token_issued_at is None:
        messages.warning(
            request, _("Gateway %(label)s has no token to revoke.") % {"label": gateway.label}
        )
    else:
        services.revoke_gateway(gateway, actor=request.user, reason=reason)
        messages.success(
            request,
            _("Gateway %(label)s revoked: the tracker refuses its token.")
            % {"label": gateway.label},
        )
    return redirect("presence:site_setup", pk=gateway.site_id)


# --- BLE badges ------------------------------------------------------------------------------


@role_required(*MANAGERS)
def badges(request):
    sites = _sites(request.user)
    today = dates.company_today()
    form = BadgeFilterForm(request.GET or None, sites=sites)
    workers = _workers(request.user, today).filter(is_active=True).select_related("trade")
    site = None
    if form.is_valid():
        text = form.cleaned_data["q"].strip()
        if text:
            by_badge = BleBadge.objects.filter(
                active_to__isnull=True, badge_id__contains=text.lower()
            ).values("worker_id")
            workers = workers.search(text) | workers.filter(pk__in=by_badge)
        site = form.cleaned_data["site"]
        if site is not None:
            on_site = SiteAssignment.objects.active_on(today).filter(site=site)
            workers = workers.filter(pk__in=on_site.values("worker_id"))
    workers = list(workers.distinct().order_by("last_name", "first_name", "pk")[:BADGE_ROWS])
    active = {
        badge.worker_id: badge
        for badge in BleBadge.objects.filter(worker__in=workers, active_to__isnull=True)
    }
    rows = [{"worker": w, "badge": active.get(w.pk)} for w in workers]
    return _page(
        request,
        "presence/badges.html",
        {
            "form": form,
            "rows": rows,
            "site": site,
            "with_badge": sum(1 for row in rows if row["badge"] is not None),
            "limit": BADGE_ROWS,
        },
    )


@role_required(*MANAGERS)
def worker_badge(request, pk):
    worker = _worker_or_404(request.user, pk)
    form = BleBadgeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            badge = services.assign_ble_badge(
                worker, form.cleaned_data["badge_id"], actor=request.user
            )
        except ValidationError as exc:
            _add_errors(form, exc)
        else:
            messages.success(
                request,
                _("Badge %(badge)s given to %(name)s.")
                % {"badge": badge.badge_id, "name": worker.full_name},
            )
            return redirect("presence:worker_badge", pk=worker.pk)
    history = list(BleBadge.objects.filter(worker=worker).order_by("-active_from", "-pk"))
    return _page(
        request,
        "presence/worker_badge.html",
        {
            "worker": worker,
            "form": form,
            "current": next((b for b in history if b.active_to is None), None),
            "history": history,
            "tracking_on": bool(resolve_setting("tracking_enabled", worker=worker)),
            "nav_active": NAV,
        },
    )


@require_POST
@role_required(*MANAGERS)
def badge_end(request, pk):
    badge = (
        BleBadge.objects.filter(worker__in=_workers(request.user), pk=pk)
        .select_related("worker")
        .first()
    )
    if badge is None:
        raise Http404
    if badge.active_to is None:
        services.end_ble_badge(badge, actor=request.user)
        messages.success(
            request,
            _("Badge %(badge)s of %(name)s ended.")
            % {"badge": badge.badge_id, "name": badge.worker.full_name},
        )
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("presence:worker_badge", pk=badge.worker_id)


# --- the browser's tracker token ----------------------------------------------------------------


def _tracker_token(user):
    """``(token, expires_at)`` for the browser, or ``(None, None)`` when there is no
    tracker to talk to (no public URL) or the user has no company."""
    if not settings.TRACKER_PUBLIC_URL:
        return None, None
    try:
        return services.mint_dashboard_token(user)
    except PermissionError:
        return None, None


@never_cache
@require_GET
def dashboard_token(request):
    """A fresh dashboard token for the live map and the timeline (session auth, JSON).

    The page asks a few minutes before its token expires. 401 when the session is gone
    (the page then asks to sign in again), 403 for a role without presence screens.
    """
    user = request.user
    if not user.is_authenticated:
        return JsonResponse(
            {"code": "not_authenticated", "message": _("Sign in again."), "details": {}},
            status=401,
        )
    if not has_role(user, *MANAGERS):
        return JsonResponse(
            {"code": "permission_denied", "message": _("Not allowed."), "details": {}},
            status=403,
        )
    if not settings.TRACKER_PUBLIC_URL:
        return JsonResponse(
            {
                "code": "tracker_not_configured",
                "message": _("The tracker is not configured on this server."),
                "details": {},
            },
            status=503,
        )
    token, expires_at = _tracker_token(user)
    if token is None:
        return JsonResponse(
            {"code": "permission_denied", "message": _("Not allowed."), "details": {}},
            status=403,
        )
    return JsonResponse(
        {
            "token": token,
            "expires_at": int(expires_at.timestamp()),
            "tracker_url": tracker_public_url(),
        }
    )


# --- live map ------------------------------------------------------------------------------------


def _people_names(user, site, today) -> dict[str, dict]:
    """``"worker:<id>"`` / ``"foreman:<id>"`` → name, detail and timeline link, from
    Django (never from the tracker)."""
    workers, foremen = selectors.site_people(user, site, today)
    admin = is_admin(user)
    names = {}
    for worker in workers:
        detail = worker.number + (f" · {worker.trade.name}" if worker.trade_id else "")
        names[f"worker:{worker.pk}"] = {
            "name": worker.full_name,
            "detail": detail,
            "url": reverse("presence:person_day", args=["worker", worker.pk, today]),
        }
    for foreman in foremen:
        names[f"foreman:{foreman.pk}"] = {
            "name": _subject_name(foreman),
            "detail": _("Foreman"),
            "url": reverse("presence:person_day", args=["foreman", foreman.pk, today])
            if admin
            else "",
        }
    return names


def _live_strings() -> dict:
    return {
        "unknownWorker": _("Worker no. {id}"),
        "unknownForeman": _("Foreman no. {id}"),
        "wholeSite": _("Whole site"),
        "onSiteTooltip": _("{count} on site"),
        "foreman": _("Foreman"),
    }


@never_cache
@role_required(*MANAGERS)
def live(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load(site.company_id)
    today = dates.company_today(company)
    tracked = selectors.is_tracked(site, company)
    geofence = SiteGeofence.objects.filter(site=site).first()
    names = _people_names(request.user, site, today)
    token, expires_at = _tracker_token(request.user) if tracked else (None, None)
    config = None
    if token:
        config = {
            "siteId": site.pk,
            "trackerUrl": tracker_public_url(),
            "token": token,
            "expiresAt": int(expires_at.timestamp()),
            "tokenUrl": reverse("presence:dashboard_token"),
            "timeZone": company.time_zone or "UTC",
            "geofence": {
                "lat": float(geofence.latitude),
                "lng": float(geofence.longitude),
                "radius": float(geofence.radius_m),
            }
            if geofence
            else None,
            "people": names,
            "zones": dict(Zone.objects.filter(site=site).values_list("code", "name")),
            "strings": _live_strings(),
        }
    seen = [
        {"day": day, "person": names.get(f"{day.subject_kind}:{day.subject_id}")}
        for day in selectors.day_sessions(site, today)
    ]
    return _page(
        request,
        "presence/live.html",
        {
            "site": site,
            "today": today,
            "tracked": tracked,
            "tracker_public": bool(settings.TRACKER_PUBLIC_URL),
            "geofence": geofence,
            "config_json": json.dumps(config) if config else "",
            "seen": seen,
            "can_manage": can_manage_site(request.user, site),
            "nav_active": NAV,
        },
    )


# --- a person's day ---------------------------------------------------------------------------


def _person_or_404(user, kind: str, pk: int, today):
    """``(subject, name, detail)``: a worker ``user`` may see, or (admins only) a foreman
    of the company."""
    if kind == SubjectKind.WORKER:
        worker = _workers(user, today).select_related("trade").filter(pk=pk).first()
        if worker is None:
            raise Http404
        detail = worker.number + (f" · {worker.trade.name}" if worker.trade_id else "")
        return worker, worker.full_name, detail
    if kind == SubjectKind.FOREMAN:
        if not is_admin(user):
            raise PermissionDenied
        foreman = company_users(user).filter(pk=pk, role=User.Role.FOREMAN).first()
        if foreman is None:
            raise Http404
        return foreman, _subject_name(foreman), _("Foreman")
    raise Http404


@never_cache
@role_required(*MANAGERS)
def person_day(request, kind, pk, date=None):
    company = CompanySettings.load()
    today = dates.company_today(company)
    subject, name, detail = _person_or_404(request.user, kind, pk, today)
    if date is None:  # today, or the day picked in the form
        pick = DayForm(request.GET or None)
        if pick.is_valid():
            return redirect("presence:person_day", kind=kind, pk=pk, date=pick.cleaned_data["date"])
        date = today
    sessions = list(selectors.person_sessions(request.user, kind, pk, date))
    timeline = selectors.timeline(sessions, date, company)
    reasons = dict(PresenceEvent.Reason.choices)
    retention = int(resolve_setting("track_retention_days", company=company))
    in_retention = today - date <= datetime.timedelta(days=retention) and date <= today
    config = None
    token, _expires_at = _tracker_token(request.user) if in_retention else (None, None)
    if token:
        start, end = selectors.day_bounds(date, dates.company_tz(company))
        sites = list(_sites(request.user))
        zones: dict[str, dict[str, str]] = {}
        for site_id, code, zone_name in Zone.objects.filter(site__in=sites).values_list(
            "site_id", "code", "name"
        ):
            zones.setdefault(str(site_id), {})[code] = zone_name
        config = {
            "trackerUrl": tracker_public_url(),
            "token": token,
            "kind": kind,
            "id": pk,
            "from": start.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "to": end.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z"),
            "timeZone": company.time_zone or "UTC",
            "sites": {str(site.pk): site.name for site in sites},
            "zones": zones,
            "strings": {
                "wholeSite": _("Whole site"),
                "offSite": _("Off site"),
                "phone": _("Foreman’s phone"),
                "gateway": _("Gateway"),
                "unknownSite": _("Site no. {id}"),
            },
        }
    return _page(
        request,
        "presence/person_day.html",
        {
            "kind": kind,
            "subject": subject,
            "name": name,
            "detail": detail,
            "date": date,
            "today": today,
            "previous": date - datetime.timedelta(days=1),
            "next": date + datetime.timedelta(days=1) if date < today else None,
            "timeline": timeline,
            "reasons": reasons,
            "pick": DayForm(initial={"date": date}),
            "config_json": json.dumps(config) if config else "",
            "tracker_public": bool(settings.TRACKER_PUBLIC_URL),
            "in_retention": in_retention,
            "retention_days": retention,
            "travel": is_admin(request.user),
            "nav_active": "workers:list" if kind == SubjectKind.WORKER else NAV,
        },
    )


# --- the worker's presence history -----------------------------------------------------------


@never_cache
@role_required(*MANAGERS)
def worker_export(request, pk):
    worker = _worker_or_404(request.user, pk)
    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 byte-order mark: Excel then reads the accents correctly.
    services.write_worker_presence_csv(request.user, worker, buffer)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    number = re.sub(r"[^A-Za-z0-9_-]", "", worker.number) or str(worker.pk)
    response["Content-Disposition"] = f'attachment; filename="presence-{number}.csv"'
    return response


# --- printed notice for badge holders -------------------------------------------------------


@role_required(*MANAGERS)
def notice(request):
    """The tracking notice for badge holders (design §10), A4 or US Letter, EN or FR."""
    sites = _sites(request.user)
    form = NoticeForm(request.GET or None, sites=sites)
    current = (translation.get_language() or "en")[:2]
    options = {"lang": current if current in ("en", "fr") else "en", "paper": "a4", "site": None}
    if form.is_valid():
        options.update({k: v for k, v in form.cleaned_data.items() if v})
    company = CompanySettings.load()
    site = options["site"]

    def setting(name):
        return resolve_setting(name, site=site, company=company)

    context = {
        "form": NoticeForm(initial=options, sites=sites),
        "lang": options["lang"],
        "paper": options["paper"],
        "site": site,
        "sites": sites,
        "company_name": company.company_name or str(company.company),
        "start": setting("tracking_start"),
        "end": setting("tracking_end"),
        "retention_days": int(resolve_setting("track_retention_days", company=company)),
        "version": selectors.current_text_version(),
    }
    with translation.override(options["lang"]):
        return _page(request, "presence/notice.html", context)
