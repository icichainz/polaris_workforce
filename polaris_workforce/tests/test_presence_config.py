"""Site configs pushed to the tracker: the payload, the PUT, and when it is queued.

What must hold:
- The body is the tracker's ``SiteConfig`` (validated against ``tracker-openapi.yaml``):
  geofence from ``checkin.SiteGeofence``, zones, gateways with a live token, badges of
  active workers with tracking on only, rules from the resolved settings.
- It goes out as ``PUT /internal/sites/{id}/config`` with a fresh 5-minute service token.
- A change to a tracking setting, a site's geofence, a zone, a gateway or a badge pushes
  the configs it alters once the change commits (nothing on a rollback); without
  ``TRACKER_URL`` nothing is sent; the periodic resync pushes every tracked site again.
"""

import datetime
import io
import json
import logging
import urllib.error
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
import yaml
from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from jsonschema import Draft202012Validator

from apps.checkin import services as checkin_services
from apps.checkin.models import SiteGeofence
from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence import services, tasks, tokens
from apps.presence.models import BleBadge, Gateway, TrackerSync, TrackingConsent, Zone
from apps.workers import services as worker_services
from apps.worksites import services as site_services
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    ForemanFactory,
    SiteFactory,
    WorkerFactory,
)
from tests.test_settings_view import form_data

pytestmark = pytest.mark.django_db

TRACKER_SPEC = Path(django_settings.BASE_DIR).parent / "docs" / "api" / "tracker-openapi.yaml"
TRACKER = "http://tracker.test:4100"
VERSION = django_settings.TRACKING_CONSENT_TEXT_VERSION


def site_config_validator() -> Draft202012Validator:
    spec = yaml.safe_load(TRACKER_SPEC.read_text())
    schema = {"$ref": "#/components/schemas/SiteConfig", "components": spec["components"]}
    return Draft202012Validator(schema)


class FakeTracker:
    """Stands in for ``urllib.request.urlopen``: records each request, answers 204."""

    def __init__(self, status=204, error=None):
        self.requests = []
        self.status = status
        self.error = error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        response = mock.MagicMock()
        response.status = self.status
        response.__enter__.return_value = response
        return response

    @property
    def sites(self) -> list[int]:
        return [int(r.full_url.split("/")[-2]) for r in self.requests]

    def body(self, index=-1) -> dict:
        return json.loads(self.requests[index].data)


@pytest.fixture(autouse=True)
def _no_pushes_left_over():
    """Pushes queued by earlier tests whose transaction rolled back (their on-commit
    callback never ran) would ride along with the first push here."""
    services._pending_sites().clear()


@pytest.fixture
def fake_tracker(settings, monkeypatch):
    settings.TRACKER_URL = TRACKER
    fake = FakeTracker()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


@pytest.fixture
def admin(default_company):
    return AdminUserFactory()


@pytest.fixture
def tracked(default_company):
    """A tracked site with a geofence, two zones, gateways and badges."""
    company = CompanySettings.load(default_company)
    company.tracking_enabled = True
    company.presence_absence_to_leave_seconds = 600
    company.save()
    site = SiteFactory(name="North", presence_sightings_to_enter=3)
    SiteGeofence.objects.create(
        site=site, latitude=Decimal("6.131900"), longitude=Decimal("1.222800"), radius_m=150
    )
    gate = Zone.objects.create(site=site, code="A", name="Gate")
    Zone.objects.create(site=site, code="B", name="Block B")
    live = Gateway.objects.create(
        site=site, zone=gate, label="Gate", token_issued_at=timezone.now()
    )
    Gateway.objects.create(site=site, label="Spare")  # never given a token
    Gateway.objects.create(
        site=site, label="Old", token_issued_at=timezone.now(), revoked_at=timezone.now()
    )
    yao = WorkerFactory(first_name="Yao")
    ama = WorkerFactory(first_name="Ama", tracking_enabled=False)
    gone = WorkerFactory(first_name="Kofi", is_active=False)
    for worker, badge_id in [(yao, "BDG-00F1"), (ama, "BDG-0A22"), (gone, "BDG-0B33")]:
        BleBadge.objects.create(company_id=site.company_id, worker=worker, badge_id=badge_id)
    BleBadge.objects.create(
        company_id=site.company_id,
        worker=WorkerFactory(),
        badge_id="BDG-OLD",
        active_to=timezone.now() - datetime.timedelta(days=1),
    )
    return {"site": site, "live": live, "yao": yao, "ama": ama, "gate": gate}


# --- the payload ---------------------------------------------------------------------------------


def test_site_config_is_the_tracker_schema_with_only_tracked_badges(tracked):
    config = services.site_config(tracked["site"])

    site_config_validator().validate(config)
    assert config == {
        "tracking_enabled": True,
        "geofence": {"lat": 6.1319, "lng": 1.2228, "radius_m": 150.0},
        "zones": [{"id": "A", "name": "Gate"}, {"id": "B", "name": "Block B"}],
        "gateways": [{"id": f"gw-{tracked['live'].pk}-0", "zone_id": "A"}],
        "badges": [{"badge_id": "bdg-00f1", "worker_id": tracked["yao"].pk}],
        "foremen": [],
        "rules": {
            "sightings_to_enter": 3,
            "arrival_window_seconds": 300,
            "absence_to_leave_seconds": 600,
        },
    }


def test_a_site_without_geofence_is_tracked_by_gateways_only(tracked):
    SiteGeofence.objects.filter(site=tracked["site"]).delete()
    config = services.site_config(tracked["site"])

    site_config_validator().validate(config)
    assert config["geofence"] is None


def test_tracking_off_on_the_site_sends_nothing_but_the_switch(tracked):
    site = tracked["site"]
    site.tracking_enabled = False
    site.save()

    config = services.site_config(site)

    site_config_validator().validate(config)
    assert config["tracking_enabled"] is False
    assert config["zones"] == config["gateways"] == config["badges"] == config["foremen"] == []
    assert config["geofence"] is None


def test_an_inactive_company_sends_nothing_but_the_switch(tracked):
    site = tracked["site"]
    site.company.is_active = False
    site.company.save()

    config = services.site_config(site)

    site_config_validator().validate(config)
    assert config["tracking_enabled"] is False
    assert config["zones"] == config["gateways"] == config["badges"] == config["foremen"] == []


def consent(user, accepted=True, version=VERSION) -> TrackingConsent:
    return TrackingConsent.objects.create(
        company_id=user.company_id,
        user=user,
        accepted=accepted,
        text_version=version,
        client_uuid=uuid.uuid4(),
    )


def test_the_config_lists_the_site_foremen_who_accepted_the_current_notice(tracked):
    accepted, refused, old_notice, never, gone, elsewhere = (
        ForemanFactory(first_name=name)
        for name in ("Kodjo", "Afi", "Komi", "Edem", "Kossi", "Mawuli")
    )
    gone.is_active = False
    gone.save()
    site = tracked["site"]
    site.foremen.set([accepted, refused, old_notice, never, gone])
    SiteFactory(foremen=[elsewhere])
    for foreman in (accepted, gone, elsewhere):
        consent(foreman)
    consent(refused)
    consent(refused, accepted=False)  # withdrawn since
    consent(old_notice, version="2025-01-01")

    config = services.site_config(site)

    site_config_validator().validate(config)
    assert config["foremen"] == [accepted.pk]


def test_a_consent_answer_pushes_the_foreman_tracked_sites(
    tracked, fake_tracker, django_capture_on_commit_callbacks
):
    foreman = ForemanFactory()
    site = tracked["site"]
    untracked = SiteFactory(name="Off", tracking_enabled=False)
    site.foremen.add(foreman)
    untracked.foremen.add(foreman)
    SiteFactory(name="South")  # tracked, not his

    with django_capture_on_commit_callbacks(execute=True):
        services.record_consent(
            foreman, client_uuid=uuid.uuid4(), accepted=True, text_version=VERSION
        )
    assert fake_tracker.sites == [site.pk]
    assert fake_tracker.body()["foremen"] == [foreman.pk]

    fake_tracker.requests.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.record_consent(
            foreman, client_uuid=uuid.uuid4(), accepted=False, text_version=VERSION
        )
    assert fake_tracker.sites == [site.pk]
    assert fake_tracker.body()["foremen"] == []


def test_assigning_a_foreman_pushes_the_site(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    foreman = ForemanFactory()
    consent(foreman)
    site = tracked["site"]

    with django_capture_on_commit_callbacks(execute=True):
        site_services.save_site(site, actor=admin, foremen=[foreman])

    assert fake_tracker.sites == [site.pk]
    assert fake_tracker.body()["foremen"] == [foreman.pk]


def test_badges_of_another_company_are_never_in_the_config(tracked):
    with use_company(CompanyFactory(name="Bravo")) as bravo:
        BleBadge.objects.create(company=bravo, worker=WorkerFactory(), badge_id="BDG-BRAVO")

    badges = services.site_config(tracked["site"])["badges"]

    assert [b["badge_id"] for b in badges] == ["bdg-00f1"]


def test_a_site_switched_off_sends_no_badge_even_of_a_worker_switched_on(tracked):
    tracked["yao"].tracking_enabled = True
    tracked["yao"].save()
    site = tracked["site"]
    site.tracking_enabled = False
    site.save()
    # The site switch decides for the whole config: no ingest at all on that site.
    assert services.site_config(site)["badges"] == []


def test_a_worker_switched_on_is_tracked_on_a_site_of_a_company_switched_off(tracked):
    company = CompanySettings.load()
    company.tracking_enabled = False
    company.save()
    site = tracked["site"]
    site.tracking_enabled = True
    site.save()
    tracked["ama"].tracking_enabled = None  # inherits the site: on
    tracked["ama"].save()

    badges = [b["badge_id"] for b in services.site_config(site)["badges"]]

    assert badges == ["bdg-00f1", "bdg-0a22"]


# --- the PUT ------------------------------------------------------------------------------------


def test_push_puts_the_config_with_a_fresh_service_token(tracked, fake_tracker):
    site = tracked["site"]

    assert services.push_site_config(site) is True

    (request,) = fake_tracker.requests
    assert request.get_method() == "PUT"
    assert request.full_url == f"{TRACKER}/internal/sites/{site.pk}/config"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == services.site_config(site)
    token = request.get_header("Authorization").removeprefix("Bearer ")
    claims = tokens.decode(token, audience="polaris-tracker")
    assert claims["kind"] == "service" and claims["sub"] == "service:django"
    assert 0 < claims["exp"] - timezone.now().timestamp() <= 300
    sync = TrackerSync.objects.get(site=site)
    assert sync.pushed_enabled is True and sync.last_error == ""
    assert sync.last_success_at is not None


def test_without_tracker_url_nothing_is_sent(tracked, settings, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="apps.presence.services")
    settings.TRACKER_URL = ""
    urlopen = mock.Mock()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert services.push_site_config(tracked["site"]) is False

    urlopen.assert_not_called()
    assert "TRACKER_URL is not set" in caplog.text


def test_an_unreachable_tracker_is_recorded(tracked, fake_tracker):
    fake_tracker.error = urllib.error.URLError("connection refused")

    with pytest.raises(services.TrackerUnreachable):
        services.push_site_config(tracked["site"])

    sync = TrackerSync.objects.get(site=tracked["site"])
    assert "connection refused" in sync.last_error
    assert sync.last_success_at is None


def test_a_refused_config_is_a_push_error(tracked, fake_tracker):
    fake_tracker.error = urllib.error.HTTPError(
        TRACKER, 400, "Bad Request", {}, io.BytesIO(b'{"code":"invalid_body"}')
    )

    with pytest.raises(services.TrackerPushError) as raised:
        services.push_site_config(tracked["site"])

    assert not isinstance(raised.value, services.TrackerUnreachable)
    assert TrackerSync.objects.get(site=tracked["site"]).last_error == "HTTP 400"


# --- what queues a push -------------------------------------------------------------------------


def test_a_company_tracking_setting_pushes_every_tracked_site(
    tracked, fake_tracker, admin, client, django_capture_on_commit_callbacks
):
    other = SiteFactory(name="South")
    client.force_login(admin)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("core:settings"), form_data(foreman_gps_interval_seconds="45")
        )

    assert response.status_code == 302
    assert sorted(fake_tracker.sites) == sorted([tracked["site"].pk, other.pk])


def test_turning_tracking_off_for_the_company_pushes_the_sites_tracked_before(
    tracked, fake_tracker, admin, client, django_capture_on_commit_callbacks
):
    services.push_site_config(tracked["site"])  # the tracker knows the site as tracked
    fake_tracker.requests.clear()
    client.force_login(admin)

    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("core:settings"), form_data(tracking_enabled=None))

    assert fake_tracker.sites == [tracked["site"].pk]
    assert fake_tracker.body()["tracking_enabled"] is False


def test_other_company_settings_push_nothing(
    tracked, fake_tracker, admin, client, django_capture_on_commit_callbacks
):
    client.force_login(admin)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("core:settings"), form_data(country="Bénin"))
    assert fake_tracker.requests == []


def test_a_site_tracking_override_pushes_that_site(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    other = SiteFactory(name="South")
    with django_capture_on_commit_callbacks(execute=True):
        site_services.update_site_settings(
            other, {"presence_absence_to_leave_seconds": 1200}, actor=admin
        )

    assert fake_tracker.sites == [other.pk]
    assert fake_tracker.body()["rules"]["absence_to_leave_seconds"] == 1200


def test_a_site_pay_setting_pushes_nothing(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        site_services.update_site_settings(
            tracked["site"], {"pay_frequency": "weekly"}, actor=admin
        )
    assert fake_tracker.requests == []


def test_a_geofence_change_pushes_the_site(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    site = tracked["site"]
    with django_capture_on_commit_callbacks(execute=True):
        checkin_services.set_geofence(
            site, latitude=Decimal("6.2"), longitude=Decimal("1.3"), radius_m=300, actor=admin
        )

    assert fake_tracker.sites == [site.pk]
    assert fake_tracker.body()["geofence"] == {"lat": 6.2, "lng": 1.3, "radius_m": 300.0}


def test_a_worker_switched_off_leaves_the_badges(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        worker_services.update_worker(tracked["yao"], {"tracking_enabled": False}, actor=admin)

    assert fake_tracker.sites == [tracked["site"].pk]
    assert fake_tracker.body()["badges"] == []


def test_a_worker_without_a_badge_pushes_nothing(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    worker = WorkerFactory()
    with django_capture_on_commit_callbacks(execute=True):
        worker_services.update_worker(worker, {"tracking_enabled": False}, actor=admin)
    assert fake_tracker.requests == []


def test_several_changes_in_one_transaction_push_each_site_once(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    site = tracked["site"]
    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        services.save_zone(Zone(site=site, code="C", name="Store"), actor=admin)
        services.save_gateway(Gateway(site=site, label="Store"), actor=admin)
        site_services.update_site_settings(site, {"tracking_start": datetime.time(5)}, actor=admin)

    assert fake_tracker.sites == [site.pk]


def test_a_change_that_rolls_back_pushes_nothing(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            services.save_zone(Zone(site=tracked["site"], code="C", name="Store"), actor=admin)
            raise RuntimeError
    assert fake_tracker.requests == []


def test_with_a_broker_the_push_is_a_task(
    tracked, settings, admin, monkeypatch, django_capture_on_commit_callbacks
):
    settings.TRACKER_URL = TRACKER
    settings.CELERY_BROKER_URL = "memory://"
    sent = []
    monkeypatch.setattr(
        tasks.push_site_config_task, "apply_async", lambda args, countdown: sent.append(args[0])
    )
    with django_capture_on_commit_callbacks(execute=True):
        services.save_zone(Zone(site=tracked["site"], code="C", name="Store"), actor=admin)
    assert sent == [tracked["site"].pk]


def test_the_task_pushes_the_site(tracked, fake_tracker):
    tasks.push_site_config_task(tracked["site"].pk)
    assert fake_tracker.sites == [tracked["site"].pk]


def test_a_failed_inline_push_never_fails_the_change(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    fake_tracker.error = urllib.error.URLError("down")
    with django_capture_on_commit_callbacks(execute=True):
        zone = services.save_zone(Zone(site=tracked["site"], code="C", name="Store"), actor=admin)
    assert Zone.objects.filter(pk=zone.pk).exists()


def test_an_unexpected_push_error_is_logged_and_the_other_sites_still_pushed(
    tracked, fake_tracker, admin, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    first, second = tracked["site"], SiteFactory(name="South")
    real_push = services.push_site_config

    def push(site):
        if site.pk == first.pk:
            raise RuntimeError("a bug in the config")
        return real_push(site)

    monkeypatch.setattr(services, "push_site_config", push)
    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        zone = services.save_zone(Zone(site=first, code="C", name="Store"), actor=admin)
        services.save_zone(Zone(site=second, code="C", name="Store"), actor=admin)

    assert Zone.objects.filter(pk=zone.pk).exists()
    assert fake_tracker.sites == [second.pk]
    assert f"config push of site {first.pk} failed" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_a_push_error_after_a_request_commits_is_not_a_server_error(
    tracked, fake_tracker, admin, client, monkeypatch
):
    """Real commits: the push runs inside the request, once the save committed."""
    send = mock.Mock(side_effect=RuntimeError("a bug in the push"))
    monkeypatch.setattr(services, "_send_push", send)
    client.force_login(admin)

    response = client.post(reverse("core:settings"), form_data(foreman_gps_interval_seconds="45"))

    assert response.status_code == 302
    assert send.called
    assert CompanySettings.load(fresh=True).foreman_gps_interval_seconds == 45


# --- gateways and badges ------------------------------------------------------------------------


def test_revoking_a_gateway_removes_it_and_a_new_token_changes_its_id(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    gateway = tracked["live"]
    with django_capture_on_commit_callbacks(execute=True):
        services.revoke_gateway(gateway, actor=admin, reason="Stolen")
    assert fake_tracker.body()["gateways"] == []

    with django_capture_on_commit_callbacks(execute=True):
        token, _ = services.issue_gateway_token(gateway, actor=admin)
    assert fake_tracker.body()["gateways"] == [{"id": f"gw-{gateway.pk}-1", "zone_id": "A"}]
    assert tokens.decode(token, audience="polaris-tracker")["sub"] == f"gateway:gw-{gateway.pk}-1"
    actions = list(
        AuditLog.objects.filter(action__startswith="presence.gateway").values_list(
            "action", flat=True
        )
    )
    assert sorted(actions) == ["presence.gateway.revoke", "presence.gateway.token_issue"]
    assert AuditLog.objects.get(action="presence.gateway.revoke").reason == "Stolen"


def test_a_gateway_zone_must_be_on_its_site(tracked, admin):
    elsewhere = Zone.objects.create(site=SiteFactory(), code="A", name="Gate")
    with pytest.raises(ValidationError):
        services.save_gateway(Gateway(site=tracked["site"], zone=elsewhere, label="X"), actor=admin)


def test_giving_a_badge_ends_the_old_one_and_is_audited(
    tracked, fake_tracker, admin, django_capture_on_commit_callbacks
):
    yao = tracked["yao"]
    with django_capture_on_commit_callbacks(execute=True):
        badge = services.assign_ble_badge(yao, " BDG-1234 ", actor=admin)

    assert badge.badge_id == "bdg-1234"
    assert BleBadge.objects.get(badge_id="bdg-00f1").active_to is not None
    assert {"badge_id": "bdg-1234", "worker_id": yao.pk} in fake_tracker.body()["badges"]
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"presence.badge.assign", "presence.badge.end"} <= actions

    with django_capture_on_commit_callbacks(execute=True):
        services.end_ble_badge(badge, actor=admin)
    assert fake_tracker.body()["badges"] == []


def test_a_badge_another_worker_holds_is_refused(tracked, admin):
    with pytest.raises(ValidationError):
        services.assign_ble_badge(WorkerFactory(), "BDG-00F1", actor=admin)
    with pytest.raises(ValidationError):
        services.assign_ble_badge(WorkerFactory(), " bdg-00f1", actor=admin)  # same, any case


def test_the_phone_ibeacon_badge_ids_are_kept_normalized(tracked, admin):
    """The phone reports ``ib:<uuid>:<major>:<minor>``, lower case with dashes."""
    typed = "IB:F7826DA6-4FA2-4E98-8024-BC5B71E0893E:100:7"
    badge = services.assign_ble_badge(tracked["yao"], typed, actor=admin)

    assert badge.badge_id == "ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7"
    assert badge.badge_id in [
        b["badge_id"] for b in services.site_config(tracked["site"])["badges"]
    ]
    assert services.assign_ble_badge(tracked["yao"], badge.badge_id, actor=admin) == badge
    with pytest.raises(ValidationError):
        services.assign_ble_badge(tracked["yao"], "x" * 65, actor=admin)


def test_the_same_badge_id_may_be_used_in_another_company(tracked):
    with use_company(CompanyFactory(name="Bravo")):
        badge = services.assign_ble_badge(WorkerFactory(), "BDG-00F1")
    assert badge.pk


# --- the periodic resync ------------------------------------------------------------------------


def test_the_beat_schedule_resyncs_the_configs():
    entry = django_settings.CELERY_BEAT_SCHEDULE["resync-tracker-configs"]
    assert entry["task"] == "presence.resync_site_configs"


def test_the_resync_pushes_tracked_sites_and_those_tracked_before(tracked, fake_tracker):
    untracked = SiteFactory(name="Off", tracking_enabled=False)
    was_tracked = SiteFactory(name="Was", tracking_enabled=False)
    TrackerSync.objects.create(site=was_tracked, pushed_enabled=True)
    with use_company(CompanyFactory(name="Bravo")) as bravo:
        bravo_settings = CompanySettings.load(bravo)
        bravo_settings.tracking_enabled = True
        bravo_settings.save()
        bravo_site = SiteFactory()

    assert tasks.resync_site_configs() == 3

    assert sorted(fake_tracker.sites) == sorted([tracked["site"].pk, was_tracked.pk, bravo_site.pk])
    assert untracked.pk not in fake_tracker.sites
    assert TrackerSync.objects.get(site=was_tracked).pushed_enabled is False


def test_the_resync_turns_tracking_off_for_an_inactive_company(tracked, fake_tracker):
    with use_company(CompanyFactory(name="Bravo")) as bravo:
        bravo_settings = CompanySettings.load(bravo)
        bravo_settings.tracking_enabled = True
        bravo_settings.save()
        pushed_on, failed, never = SiteFactory(), SiteFactory(), SiteFactory()
    TrackerSync.objects.create(site=pushed_on, pushed_enabled=True)
    TrackerSync.objects.create(site=failed, last_error="HTTP 503")
    bravo.is_active = False
    bravo.save()

    assert sorted(site.pk for site in services.sites_to_resync()) == sorted(
        [tracked["site"].pk, pushed_on.pk, failed.pk]
    )
    tasks.resync_site_configs()

    assert never.pk not in fake_tracker.sites
    bodies = {site_id: fake_tracker.body(i) for i, site_id in enumerate(fake_tracker.sites)}
    assert bodies[pushed_on.pk]["tracking_enabled"] is False
    assert bodies[pushed_on.pk]["badges"] == bodies[pushed_on.pk]["foremen"] == []
    assert TrackerSync.objects.get(site=pushed_on).pushed_enabled is False


def test_the_resync_stops_at_an_unreachable_tracker(tracked, fake_tracker):
    SiteFactory(name="South")
    fake_tracker.error = urllib.error.URLError("down")

    assert tasks.resync_site_configs() == 0
    assert len(fake_tracker.requests) == 1


def test_the_command_pushes_one_site(tracked, fake_tracker):
    out = io.StringIO()
    call_command("push_tracker_config", "--site", str(tracked["site"].pk), stdout=out)
    assert fake_tracker.sites == [tracked["site"].pk]
    assert "1 pushed" in out.getvalue()
