"""DeviceTokenAuthentication.

Tests that need ``accounts.ForemanDevice`` skip until F1's model is merged; they then run
unchanged. They use only the fields from F1's brief: user, device_id, token_hash,
last_seen_at, revoked_at.
"""

import hashlib
from datetime import timedelta

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.test import APIClient, APIRequestFactory

from apps.api.authentication import (
    DeviceTokenAuthentication,
    generate_device_token,
    hash_device_token,
)

factory = APIRequestFactory()


def _request(header=None):
    extra = {"HTTP_AUTHORIZATION": header} if header is not None else {}
    return factory.get("/api/v1/me/", **extra)


# --- Without the model -------------------------------------------------------------------


def test_generate_device_token_returns_raw_and_sha256_hash():
    raw, token_hash = generate_device_token()

    assert len(raw) >= 40
    assert token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert token_hash == hash_device_token(raw)
    assert generate_device_token()[0] != raw


@pytest.mark.parametrize("header", [None, "", "Bearer abc", "Token abc", "Basic dXNlcjpwdw=="])
def test_other_schemes_are_left_to_other_authenticators(header):
    assert DeviceTokenAuthentication().authenticate(_request(header)) is None


@pytest.mark.parametrize("header", ["Device", "Device a b"])
def test_malformed_device_header_is_rejected(header):
    with pytest.raises(exceptions.AuthenticationFailed):
        DeviceTokenAuthentication().authenticate(_request(header))


def test_authenticate_header_makes_401():
    assert DeviceTokenAuthentication().authenticate_header(_request()) == "Device"


# --- With ForemanDevice (F1) -------------------------------------------------------------


@pytest.fixture
def device_model(db):
    try:
        return apps.get_model("accounts", "ForemanDevice")
    except LookupError:
        pytest.skip("accounts.ForemanDevice is not merged yet (F1)")


@pytest.fixture
def foreman(db):
    return get_user_model().objects.create_user(username="kossi", password="s3cret-pass")


@pytest.fixture
def device_and_token(device_model, foreman):
    raw, token_hash = generate_device_token()
    device = device_model.objects.create(
        user=foreman, device_id="0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11", token_hash=token_hash
    )
    return device, raw


def test_valid_token_authenticates_user_and_device(device_and_token, foreman):
    device, raw = device_and_token

    user, auth = DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))

    assert user == foreman
    assert auth.pk == device.pk


def test_keyword_is_case_insensitive(device_and_token, foreman):
    _, raw = device_and_token

    user, _ = DeviceTokenAuthentication().authenticate(_request(f"device {raw}"))

    assert user == foreman


def test_wrong_token_is_rejected(device_and_token):
    with pytest.raises(exceptions.AuthenticationFailed):
        DeviceTokenAuthentication().authenticate(_request("Device not-the-token"))


def test_revoked_token_is_rejected(device_and_token):
    device, raw = device_and_token
    device.revoked_at = timezone.now()
    device.save()

    with pytest.raises(exceptions.AuthenticationFailed):
        DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))


def test_inactive_user_is_rejected(device_and_token, foreman):
    _, raw = device_and_token
    foreman.is_active = False
    foreman.save()

    with pytest.raises(exceptions.AuthenticationFailed):
        DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))


def test_last_seen_is_set_on_first_use(device_and_token, device_model):
    device, raw = device_and_token
    assert device.last_seen_at is None

    DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))

    assert device_model.objects.get(pk=device.pk).last_seen_at is not None


def test_last_seen_is_written_at_most_once_per_minute(device_and_token, device_model):
    device, raw = device_and_token
    recent = timezone.now() - timedelta(seconds=20)
    device_model.objects.filter(pk=device.pk).update(last_seen_at=recent)

    DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))

    assert device_model.objects.get(pk=device.pk).last_seen_at == recent


def test_last_seen_is_refreshed_after_a_minute(device_and_token, device_model):
    device, raw = device_and_token
    old = timezone.now() - timedelta(minutes=5)
    device_model.objects.filter(pk=device.pk).update(last_seen_at=old)

    DeviceTokenAuthentication().authenticate(_request(f"Device {raw}"))

    assert device_model.objects.get(pk=device.pk).last_seen_at > old


def test_revoked_token_gets_401_over_http(device_and_token):
    device, raw = device_and_token
    device.revoked_at = timezone.now()
    device.save()

    response = APIClient(HTTP_AUTHORIZATION=f"Device {raw}").get(reverse("api:accounts:me"))

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_valid_token_reaches_the_view_over_http(device_and_token):
    _, raw = device_and_token

    response = APIClient(HTTP_AUTHORIZATION=f"Device {raw}").get(reverse("api:accounts:me"))

    assert response.status_code == 501  # stub until Wave 2 (A)
