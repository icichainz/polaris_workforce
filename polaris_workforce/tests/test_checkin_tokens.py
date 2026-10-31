"""Badge tokens (signing, verification, revocation) and geofence maths."""

import hashlib
from decimal import Decimal

import pytest

from apps.checkin import geo, services, tokens
from apps.checkin.models import Badge, CheckIn
from apps.core.models import AuditLog
from tests.factories import SiteFactory, UserFactory, WorkerFactory

# --- tokens ---------------------------------------------------------------------------------


def test_payload_is_short_alphanumeric_and_verifies():
    public_id = tokens.new_public_id()
    payload = tokens.make_payload(public_id)

    assert payload.startswith("PW1:")
    assert len(payload) == 4 + 16 + 1 + 16
    assert set(payload) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567:.PW1")
    assert tokens.verify(payload) == public_id
    # Without the prefix, in lower case, with spaces: still the same badge.
    assert tokens.verify("  " + payload[4:].lower() + "\n") == public_id


def test_public_ids_are_random():
    assert len({tokens.new_public_id() for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p[:-1] + ("A" if p[-1] != "A" else "B"),  # signature changed
        lambda p: p[:5] + ("A" if p[5] != "A" else "B") + p[6:],  # id changed
        lambda p: p.replace(".", ":"),
        lambda p: p + "A",
        lambda p: "PW2:" + p[4:],
        lambda p: "https://example.com/" + p,
        lambda p: "",
    ],
)
def test_tampered_or_foreign_codes_do_not_verify(mangle):
    payload = tokens.make_payload(tokens.new_public_id())
    assert tokens.verify(mangle(payload)) is None


def test_signature_depends_on_the_secret_key(settings):
    payload = tokens.make_payload("K7Q2M4XR5TBN3WCA")
    settings.SECRET_KEY = "another-secret-" + "x" * 50
    assert tokens.verify(payload) is None


def test_old_secret_in_fallbacks_keeps_badges_working(settings):
    old = settings.SECRET_KEY
    payload = tokens.make_payload("K7Q2M4XR5TBN3WCA")
    settings.SECRET_KEY = "rotated-secret-" + "x" * 50
    settings.SECRET_KEY_FALLBACKS = [old]
    assert tokens.verify(payload) == "K7Q2M4XR5TBN3WCA"


def test_payload_sha256_is_the_hash_of_the_printed_text():
    payload = tokens.make_payload("K7Q2M4XR5TBN3WCA")
    assert tokens.payload_sha256(payload) == hashlib.sha256(payload.encode()).hexdigest()


# --- badges -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_issue_badge_is_audited_and_not_the_worker_id():
    actor = UserFactory()
    worker = WorkerFactory()

    badge = services.issue_badge(worker, actor)

    assert badge.is_active and badge.issued_by == actor
    assert worker.number not in badge.payload
    entry = AuditLog.objects.get(action="checkin.badge.issue")
    assert entry.after["worker_id"] == worker.pk and entry.before is None


@pytest.mark.django_db
def test_reissue_revokes_the_old_badge():
    actor = UserFactory()
    worker = WorkerFactory()
    first = services.issue_badge(worker, actor)

    second = services.issue_badge(worker, actor)

    first.refresh_from_db()
    assert first.revoked_at is not None and first.revoke_reason == "Reissued"
    assert second.is_active and second.public_id != first.public_id
    assert Badge.objects.filter(worker=worker, revoked_at__isnull=True).count() == 1


@pytest.mark.django_db
def test_only_one_active_badge_per_worker_in_the_database():
    from django.db import IntegrityError, transaction

    worker = WorkerFactory()
    Badge.objects.create(worker=worker)
    with pytest.raises(IntegrityError), transaction.atomic():
        Badge.objects.create(worker=worker)


@pytest.mark.django_db
def test_revoke_badge_and_find_badge():
    actor = UserFactory()
    worker = WorkerFactory()
    site = SiteFactory()
    badge = services.issue_badge(worker, actor)

    assert services.find_badge(site, badge.payload) == (badge, "")
    services.revoke_badge(badge, actor, "Lost")
    services.revoke_badge(badge, actor, "again")  # no second entry

    found, problem = services.find_badge(site, badge.payload)
    assert found == badge and problem == CheckIn.BadgeProblem.REVOKED
    assert AuditLog.objects.filter(action="checkin.badge.revoke").count() == 1
    assert AuditLog.objects.get(action="checkin.badge.revoke").reason == "Lost"


@pytest.mark.django_db
def test_correctly_signed_but_unknown_badge_is_unknown():
    site = SiteFactory()
    payload = tokens.make_payload(tokens.new_public_id())
    assert services.find_badge(site, payload) == (None, CheckIn.BadgeProblem.UNKNOWN)


# --- geofence maths -----------------------------------------------------------------------


def test_haversine_known_distances():
    assert geo.haversine_m(6.131944, 1.222778, 6.131944, 1.222778) == 0
    # One degree of latitude is about 111.2 km.
    assert geo.haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)
    # Lomé → Accra, about 166 km.
    assert geo.haversine_m(6.1319, 1.2228, 5.6037, -0.1870) == pytest.approx(166_500, rel=0.05)
    # Decimals (as stored) work too.
    assert geo.haversine_m(Decimal("6.1"), Decimal("1.2"), Decimal("6.1"), Decimal("1.2001")) == (
        pytest.approx(11.06, rel=0.01)
    )


def test_is_inside_with_capped_accuracy():
    assert geo.is_inside(150, 150)
    assert not geo.is_inside(151, 150)
    assert geo.is_inside(180, 150, accuracy_m=30)  # within the accuracy circle
    assert not geo.is_inside(260, 150, accuracy_m=2000)  # allowance capped at 50 m
    assert geo.is_inside(200, 150, accuracy_m=2000)


def test_check_without_fix_or_geofence():
    class Fence:
        latitude, longitude, radius_m = Decimal("6.131944"), Decimal("1.222778"), 100

    assert geo.check(None, Decimal(6), Decimal(1)) == (None, None)
    assert geo.check(Fence, None, None) == (None, None)
    distance, inside = geo.check(Fence, Decimal("6.132500"), Decimal("1.222778"))
    assert distance == 62 and inside is True
    distance, inside = geo.check(Fence, Decimal("6.140000"), Decimal("1.222778"))
    assert distance == 896 and inside is False
