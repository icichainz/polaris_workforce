"""Helpers for the end-to-end tests (``tests/test_e2e_*.py``).

The tests drive the real stack the way the two clients do:

* ``Phone``: the foreman app. Signs in with ``POST /api/v1/auth/pin/`` and sends
  ``Authorization: Device <token>`` on every call, exactly like the Flutter client.
* ``Office``: a browser session on the office dashboard, with CSRF checks **on**. Every
  POST carries the CSRF token the page gave it (form field, or the ``X-CSRFToken``
  header that ``base.html`` puts on every htmx request).

Data that the office has no screen for in the flow (users, sites, workers) comes from
the factories; everything the flow is about goes through HTTP.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field

from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.models import CompanySettings
from tests.factories import (
    AdminUserFactory,
    ForemanFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    WorkerFactory,
)
from tests.test_settings_view import form_data

PIN_URL = "/api/v1/auth/pin/"
HIRED_ON = datetime.date(2025, 1, 1)


def new_uuid() -> str:
    return str(uuid.uuid4())


# --- the phone ----------------------------------------------------------------------------


class Phone:
    """The foreman app: one device, one token."""

    def __init__(self, phone: str, pin: str, device_id: str | None = None):
        self.device_id = device_id or f"e2e-{uuid.uuid4()}"
        self.client = APIClient()
        response = self.client.post(
            PIN_URL,
            {"phone": phone, "pin": pin, "device_id": self.device_id, "device_name": "Tecno"},
            format="json",
        )
        assert response.status_code == 200, response.content
        self.sign_in = response.json()
        self.token = self.sign_in["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Device {self.token}")

    # Reads
    def me(self):
        return self.client.get("/api/v1/me/")

    def crew(self, site_id, date):
        return self.client.get(f"/api/v1/sites/{site_id}/crew/", {"date": date.isoformat()})

    def day(self, site_id, date):
        return self.client.get(f"/api/v1/sites/{site_id}/days/{date.isoformat()}/")

    def days(self, site_id, start, end):
        return self.client.get(
            f"/api/v1/sites/{site_id}/days/", {"from": start.isoformat(), "to": end.isoformat()}
        )

    def payout(self, site_id, date):
        return self.client.get(f"/api/v1/sites/{site_id}/payouts/{date.isoformat()}/")

    def sync(self, since=None):
        return self.client.get("/api/v1/sync/", {"since": since} if since else {})

    # Writes (each carries its client_uuid, so the caller can replay it)
    def put_records(self, site_id, date, records):
        return self.client.put(
            f"/api/v1/sites/{site_id}/days/{date.isoformat()}/records/",
            {"records": records},
            format="json",
        )

    def submit(self, site_id, date, client_uuid, note=""):
        return self.client.post(
            f"/api/v1/sites/{site_id}/days/{date.isoformat()}/submit/",
            {"client_uuid": client_uuid, "foreman_note": note},
            format="json",
        )

    def correct(self, record_id, client_uuid, field_name, new_value, reason):
        return self.client.post(
            f"/api/v1/records/{record_id}/corrections/",
            {
                "client_uuid": client_uuid,
                "field": field_name,
                "new_value": new_value,
                "reason": reason,
            },
            format="json",
        )

    def pay(self, line_id, amount, client_uuid, proof_type="signature", proof=None, paid_at=None):
        data = {
            "client_uuid": client_uuid,
            "pay_line_id": line_id,
            "amount": amount,
            "proof_type": proof_type,
        }
        if paid_at:
            data["paid_at"] = paid_at
        if proof is not None:
            proof.seek(0)
            data["proof_file"] = proof
            return self.client.post("/api/v1/payments/", data, format="multipart")
        return self.client.post("/api/v1/payments/", data, format="json")

    def logout(self):
        return self.client.post("/api/v1/auth/logout/")


# --- the office ---------------------------------------------------------------------------


class Office:
    """A signed-in browser on the office dashboard, with CSRF enforced."""

    def __init__(self, user):
        self.user = user
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(user)
        # Any office page sets the CSRF cookie (base.html renders {{ csrf_token }}).
        self.client.get(reverse("dashboard:home"))
        assert "csrftoken" in self.client.cookies

    @property
    def csrf(self) -> str:
        return self.client.cookies["csrftoken"].value

    def get(self, url, data=None, **extra):
        return self.client.get(url, data or {}, **extra)

    def post(self, url, data=None, *, htmx=False, **extra):
        """A form POST; with ``htmx`` the token goes in the header, like base.html does."""
        data = dict(data or {})
        if htmx:
            extra.setdefault("HTTP_HX_REQUEST", "true")
            extra.setdefault("HTTP_X_CSRFTOKEN", self.csrf)
        else:
            data.setdefault("csrfmiddlewaretoken", self.csrf)
        return self.client.post(url, data, **extra)

    # Screens used by the flow
    def save_settings(self, **changes):
        response = self.post(reverse("core:settings"), form_data(**changes))
        assert response.status_code == 302, _form_errors(response)
        return CompanySettings.load()

    def set_pin(self, user, pin):
        response = self.post(
            reverse("accounts:user_set_pin", args=[user.pk]), {"pin": pin, "pin_confirm": pin}
        )
        assert response.status_code == 302, _form_errors(response)

    def approve(self, day_id):
        response = self.post(
            reverse("attendance:decide", args=[day_id]), {"action": "approve"}, htmx=True
        )
        assert response.status_code == 204, response.content
        assert response["HX-Redirect"] == reverse("attendance:approvals")
        return response

    def issue_cash(self, site_id, date, amount_text):
        response = self.post(
            reverse("payroll:cash_issue", args=[site_id, date.isoformat()]),
            {"amount": amount_text, "note": "Envelope for the foreman"},
        )
        assert response.status_code == 302, response.content
        return response

    def lock_sheet(self, site_id, date, reason=""):
        return self.post(
            reverse("payroll:cash_sheet_lock", args=[site_id, date.isoformat()]),
            {"reason": reason},
        )


def _form_errors(response):
    context = getattr(response, "context", None)
    if context and "form" in context:
        return context["form"].errors
    return response.status_code


# --- the world ----------------------------------------------------------------------------


@dataclass
class Crew:
    site: object
    manager: object
    foreman: object
    phone_number: str
    workers: dict = field(default_factory=dict)  # key → Worker


def make_site(name, *, phone_number, pin=None, office=None, **site_kwargs):
    """A site with its manager and a foreman (PIN set on the Users screen when an admin
    office is given, else directly)."""
    manager = SiteManagerFactory()
    foreman = ForemanFactory(phone=phone_number)
    site = SiteFactory(name=name, manager=manager, started_on=HIRED_ON, **site_kwargs)
    site.foremen.add(foreman)
    if pin:
        if office is not None:
            office.set_pin(foreman, pin)
        else:
            foreman.set_pin(pin)
            foreman.save()
    return Crew(site=site, manager=manager, foreman=foreman, phone_number=phone_number)


def hire(crew: Crew, key: str, rate: int, pay_type: str = "daily", **worker_kwargs):
    worker = WorkerFactory(first_name=key, **worker_kwargs)
    PayRateFactory(worker=worker, amount=rate, pay_type=pay_type, effective_from=HIRED_ON)
    SiteAssignmentFactory(worker=worker, site=crew.site, start_date=HIRED_ON)
    crew.workers[key] = worker
    return worker


def admin_office():
    return Office(AdminUserFactory())


def record(worker, status, arrival=None, departure=None, client_uuid=None, note=""):
    return {
        "client_uuid": client_uuid or new_uuid(),
        "worker_id": worker.pk,
        "status": status,
        "arrival": arrival,
        "departure": departure,
        "note": note,
    }


def lines_by_worker(payout_json) -> dict[int, dict]:
    return {line["worker"]["id"]: line for line in payout_json["lines"]}
