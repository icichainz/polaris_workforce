"""Office pages (admins): the Messages log, messaging settings and who gets texts.

Everything is read and written inside the admin's company: messages through
``_company_messages``, people through ``company_users`` / ``company_workers``. An id from
another company answers 404.
"""

import datetime

from django.contrib import messages as flash
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.accounts.permissions import Role, company_users, company_workers, role_required
from apps.core.tenancy import current_company

from . import services
from .forms import MessageFilterForm, MessagingSettingsForm, PreferenceForm
from .models import ContactPreference, Message, MessagingSettings
from .phones import normalise_phone
from .providers import provider_name

PAGE_SIZE = 50


def _company_messages():
    return Message.objects.filter(company=current_company())


def _day_start(date: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(date, datetime.time.min))


def _int(value) -> int:
    """A posted id, or 0 (matches nothing) when it is not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _back(request, name: str):
    query = QueryDict(request.POST.get("query", "")).urlencode()
    response = redirect(name)
    if query:
        response["Location"] += f"?{query}"
    return response


@role_required(Role.ADMIN)
def messages_view(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "resend":
            message = get_object_or_404(_company_messages(), pk=_int(request.POST.get("message")))
            if services.resend(request.user, message):
                flash.success(request, _("The message is queued again."))
            else:
                flash.info(request, _("Only failed messages can be resent."))
        elif action == "send":
            summary = services.send_pending(current_company())
            flash.success(
                request,
                ngettext("%(n)s message sent.", "%(n)s messages sent.", summary.sent)
                % {"n": summary.sent},
            )
            if summary.waiting:
                flash.info(request, _("Quiet hours: queued messages wait until they end."))
            if summary.failed or summary.retrying:
                flash.warning(
                    request,
                    _("%(failed)s failed, %(retrying)s will be tried again.")
                    % {"failed": summary.failed, "retrying": summary.retrying},
                )
        else:
            raise Http404
        return _back(request, "messaging:messages")

    form = MessageFilterForm(request.GET or None)
    entries = _company_messages().select_related("user", "worker")
    if form.is_bound and form.is_valid():
        data = form.cleaned_data
        if data["status"]:
            entries = entries.filter(status=data["status"])
        if data["kind"]:
            entries = entries.filter(kind=data["kind"])
        if data["date_from"]:
            entries = entries.filter(created_at__gte=_day_start(data["date_from"]))
        if data["date_to"]:
            next_day = data["date_to"] + datetime.timedelta(days=1)
            entries = entries.filter(created_at__lt=_day_start(next_day))
    page = Paginator(entries, PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "messaging/messages.html",
        {
            "form": form,
            "page": page,
            "query": query.urlencode(),
            "full_query": request.GET.urlencode(),
            "queued": _company_messages().filter(status=Message.Status.QUEUED).count(),
            "messaging": MessagingSettings.load(current_company()),
            "provider": provider_name(),
            "tab": "messages",
        },
    )


@role_required(Role.ADMIN)
def settings_view(request):
    messaging = MessagingSettings.load(current_company())
    before = services.settings_snapshot(messaging)
    if request.method == "POST" and request.POST.get("action") == "test":
        message = services.send_test_message(request.user)
        if message is None:
            flash.error(
                request,
                _(
                    "No test message: turn messaging on, and check your phone number and "
                    "your own choice on the Recipients page."
                ),
            )
        elif message.status == Message.Status.SENT:
            flash.success(request, _("Test message sent to %(to)s.") % {"to": message.to})
        else:
            flash.warning(
                request,
                _("The test message was not sent: %(error)s") % {"error": message.error},
            )
        return redirect("messaging:settings")
    if request.method == "POST":
        form = MessagingSettingsForm(request.POST, instance=messaging)
        if form.is_valid():
            changed = services.save_settings(form, actor=request.user, before=before)
            if changed:
                flash.success(request, _("Messaging settings saved."))
            else:
                flash.info(request, _("Nothing changed."))
            return redirect("messaging:settings")
        flash.error(request, _("Some settings need fixing — see the highlighted fields."))
    else:
        form = MessagingSettingsForm(instance=messaging)
    return render(
        request,
        "messaging/settings.html",
        {
            "form": form,
            "provider": provider_name(),
            "tab": "settings",
            "nav_active": "messaging:messages",
        },
    )


def _preferences(company, *, users=None, workers=None) -> dict:
    found = ContactPreference.objects.filter(company=company)
    if users is not None:
        return {p.user_id: p for p in found.filter(user__in=users)}
    return {p.worker_id: p for p in found.filter(worker__in=workers)}


def _row(person, preference, messaging):
    allowed = services.may_message(messaging, preference)
    return {
        "person": person,
        "phone": person.phone,
        "usable": normalise_phone(person.phone, messaging.default_country_code),
        "preference": preference,
        "gets_messages": allowed and bool(person.phone),
        "opted_in": allowed,
    }


@role_required(Role.ADMIN)
def recipients_view(request):
    company = current_company()
    messaging = MessagingSettings.load(company)
    if request.method == "POST":
        opted_in = request.POST.get("opted_in") == "1"
        if request.POST.get("user"):
            person = get_object_or_404(company_users(request.user), pk=_int(request.POST["user"]))
            preference = services.preference_for(company.pk, user=person)
            services.set_preference(
                request.user,
                user=person,
                opted_in=opted_in,
                channel=preference.channel if preference else "",
            )
        elif request.POST.get("worker"):
            person = get_object_or_404(
                company_workers(request.user), pk=_int(request.POST["worker"])
            )
            preference = services.preference_for(company.pk, worker=person)
            services.set_preference(
                request.user,
                worker=person,
                opted_in=opted_in,
                channel=preference.channel if preference else "",
            )
        else:
            raise Http404
        flash.success(request, _("Saved."))
        return _back(request, "messaging:recipients")

    users = list(company_users(request.user).filter(is_active=True).order_by("first_name", "pk"))
    user_prefs = _preferences(company, users=users)
    search = (request.GET.get("q") or "").strip()
    workers = company_workers(request.user).filter(is_active=True)
    if search:
        workers = workers.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(number__icontains=search)
            | Q(phone__icontains=search)
        )
    page = Paginator(workers.order_by("last_name", "first_name", "pk"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    worker_prefs = _preferences(company, workers=list(page))
    return render(
        request,
        "messaging/recipients.html",
        {
            "user_rows": [_row(u, user_prefs.get(u.pk), messaging) for u in users],
            "worker_rows": [_row(w, worker_prefs.get(w.pk), messaging) for w in page],
            "page": page,
            "search": search,
            "query": request.GET.urlencode(),
            "messaging": messaging,
            "tab": "recipients",
            "nav_active": "messaging:messages",
        },
    )


@role_required(Role.ADMIN)
def worker_view(request, pk):
    """One worker's choice and the texts they were sent (linked from the worker profile)."""
    worker = get_object_or_404(company_workers(request.user), pk=pk)
    company = current_company()
    messaging = MessagingSettings.load(company)
    preference = services.preference_for(company.pk, worker=worker)
    if request.method == "POST":
        form = PreferenceForm(request.POST)
        if form.is_valid():
            services.set_preference(
                request.user,
                worker=worker,
                opted_in=form.cleaned_data["opted_in"],
                channel=form.cleaned_data["channel"],
            )
            flash.success(request, _("Saved."))
            return redirect("messaging:worker", pk=worker.pk)
    else:
        form = PreferenceForm(
            initial={
                "opted_in": services.may_message(messaging, preference),
                "channel": preference.channel if preference else "",
            }
        )
    sent = _company_messages().filter(worker=worker).order_by("-created_at", "-id")[:20]
    return render(
        request,
        "messaging/worker.html",
        {
            "worker": worker,
            "form": form,
            "row": _row(worker, preference, messaging),
            "sent": sent,
            "messaging": messaging,
            "nav_active": "messaging:messages",
        },
    )
