"""Check-in data for the isolation worlds (``test_e2e_isolation``, ``test_multi_company``).

``seed_checkin(crew, worker)`` gives a site a badge for ``worker``, a geofence, a kiosk
phone and a check-in with a photo, and returns their ids for the URL tables of those
modules. The test below checks the seed itself.
"""

import datetime
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import ForemanDevice
from apps.checkin import services
from apps.checkin.models import CheckIn, KioskDevice

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
LAT, LNG = "6.131944", "1.222778"


def seed_checkin(crew, worker) -> dict:
    from decimal import Decimal

    badge = services.issue_badge(worker, crew.manager)
    services.set_geofence(
        crew.site, latitude=Decimal(LAT), longitude=Decimal(LNG), radius_m=150, actor=crew.manager
    )
    device = ForemanDevice.objects.create(
        user=crew.foreman,
        device_id=f"kiosk-{uuid.uuid4()}",
        name="Gate kiosk",
        token_hash=ForemanDevice.hash_token(uuid.uuid4().hex),
    )
    services.enable_kiosk(device, crew.site, crew.manager)
    result = services.record_checkin(
        user=crew.foreman,
        device=None,
        site=crew.site,
        direction="in",
        at=timezone.now() - datetime.timedelta(minutes=30),
        client_uuid=uuid.uuid4(),
        badge=badge.payload,
        latitude=Decimal(LAT),
        longitude=Decimal(LNG),
        accuracy_m=10,
        photo=SimpleUploadedFile("gate.jpg", JPEG, content_type="image/jpeg"),
    )
    return {"checkin": result.instance.pk, "kiosk_device": device.pk, "badge": badge.pk}


@pytest.mark.django_db
def test_seed_makes_a_counted_checkin_with_a_photo(settings, tmp_path):
    from tests.e2e import hire, make_site
    from tests.factories import CompanySettingsFactory

    settings.MEDIA_ROOT = tmp_path
    CompanySettingsFactory()
    crew = make_site("Seed Site", phone_number="+228 90 09 09 09")
    worker = hire(crew, "Seed", 5000)

    ids = seed_checkin(crew, worker)

    checkin = CheckIn.objects.get(pk=ids["checkin"])
    assert checkin.counted and checkin.on_crew and checkin.inside_geofence
    assert checkin.photo.name.startswith("checkins/")
    assert KioskDevice.objects.get(device_id=ids["kiosk_device"]).site == crew.site
