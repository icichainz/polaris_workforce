"""Tokens shared with the tracker (JWT HS256, claims v2), with the standard library only.

The cross-check tokens below are built by hand with ``hmac`` from the fixed test secret
(``tests/settings.py``): the tracker's tests (``polaris_tracker``) can use the very same
strings to prove both sides agree byte for byte on the scheme.
"""

import base64
import hashlib
import hmac
import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.core.models import CompanySettings
from apps.core.tenancy import use_company
from apps.presence import services, tokens
from apps.presence.models import Gateway
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    ForemanFactory,
    SiteFactory,
    SiteManagerFactory,
)

pytestmark = pytest.mark.django_db

SECRET = b"tests-only-tracker-secret-0123456789abcdef"
EXP = 1793468000  # 2026-11-01T18:13:20Z

# claims v2 of a foreman's phone, as compact JSON in the order Django writes them.
DEVICE_CLAIMS = (
    b'{"sub":"foreman:7","kind":"device","company":1,"sites":[3],"travel":false,'
    b'"aud":"polaris-tracker","exp":1793468000}'
)
KNOWN_DEVICE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJmb3JlbWFuOjciLCJraW5kIjoiZGV2aWNlIiwiY29tcGFueSI6MSwic2l0ZXMiOlszXSwidHJh"
    "dmVsIjpmYWxzZSwiYXVkIjoicG9sYXJpcy10cmFja2VyIiwiZXhwIjoxNzkzNDY4MDAwfQ."
    "-j0wb_CttNyRomMExCxIAtr-murJFGOMEti3KkcuMUg"
)
# What the tracker sends Django: kind service, sub service:tracker, aud polaris-django.
KNOWN_TRACKER_SERVICE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJzZXJ2aWNlOnRyYWNrZXIiLCJraW5kIjoic2VydmljZSIsImF1ZCI6InBvbGFyaXMtZGphbmdvIiwi"
    "ZXhwIjoxNzkzNDY4MDAwfQ."
    "8FRlzxMqVSXI52g-NKY93akuvgBGOO87LTT6eyRaGcM"
)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def hand_made(claims: dict | bytes, secret: bytes = SECRET, header=None) -> str:
    """A JWT built without ``apps.presence.tokens``."""
    header = header or b'{"alg":"HS256","typ":"JWT"}'
    payload = claims if isinstance(claims, bytes) else json.dumps(claims).encode()
    signing_input = f"{b64(header)}.{b64(payload)}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{b64(signature)}"


# --- the scheme, byte for byte ---------------------------------------------------------------


def test_the_test_secret_is_the_fixed_one(settings):
    assert settings.TRACKER_SHARED_SECRET.encode() == SECRET


def test_minted_token_is_byte_for_byte_the_hand_made_one():
    issued = tokens.mint(
        kind="device",
        sub="foreman:7",
        company=1,
        sites=[3],
        travel=False,
        lifetime=tokens.DEVICE_LIFETIME,
        now=EXP - tokens.DEVICE_LIFETIME,
    )

    assert issued.token == hand_made(DEVICE_CLAIMS) == KNOWN_DEVICE_TOKEN
    header, payload, signature = issued.token.split(".")
    assert header == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"  # {"alg":"HS256","typ":"JWT"}
    assert "=" not in issued.token
    assert base64.urlsafe_b64decode(payload + "==") == DEVICE_CLAIMS
    assert list(issued.claims) == ["sub", "kind", "company", "sites", "travel", "aud", "exp"]


def test_the_tracker_service_token_of_the_cross_check_is_accepted():
    claims = tokens.verify_tracker_service_token(KNOWN_TRACKER_SERVICE_TOKEN, now=EXP - 300)

    assert claims == {
        "sub": "service:tracker",
        "kind": "service",
        "aud": "polaris-django",
        "exp": EXP,
    }


def test_service_tokens_django_sends_have_no_company():
    token = tokens.mint_service_token(now=EXP - 300)
    claims = tokens.decode(token, audience="polaris-tracker", now=EXP - 1)

    assert claims == {
        "sub": "service:django",
        "kind": "service",
        "sites": [],
        "travel": False,
        "aud": "polaris-tracker",
        "exp": EXP,
    }


def test_sites_are_always_an_explicit_sorted_list_and_never_all_sites():
    issued = tokens.mint(kind="dashboard", sub="user:1", company=1, sites=[9, 3, 3], lifetime=60)

    assert issued.claims["sites"] == [3, 9]
    assert "all_sites" not in issued.claims


def test_company_is_required_except_for_service_tokens():
    with pytest.raises(ValueError):
        tokens.mint(kind="device", sub="foreman:1", sites=[1], lifetime=60)


def test_without_a_secret_and_debug_off_nothing_is_signed(settings):
    settings.TRACKER_SHARED_SECRET = ""
    settings.DEBUG = False
    with pytest.raises(ImproperlyConfigured):
        tokens.mint_service_token()


def test_without_a_secret_in_debug_a_dev_secret_is_used(settings):
    settings.TRACKER_SHARED_SECRET = ""
    settings.DEBUG = True
    token = tokens.mint_service_token()
    assert tokens.decode(token, audience="polaris-tracker")["sub"] == "service:django"


# --- tokens from the tracker: what is refused ----------------------------------------------------

NOW = EXP - 300
GOOD = {"sub": "service:tracker", "kind": "service", "aud": "polaris-django", "exp": EXP}


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (hand_made(GOOD, secret=b"another-secret"), "signature"),
        (hand_made({**GOOD, "aud": "polaris-tracker"}), "not issued for this service"),
        (hand_made({**GOOD, "kind": "device"}), "Only service tokens"),
        (hand_made({**GOOD, "kind": "dashboard"}), "Only service tokens"),
        (hand_made({**GOOD, "sub": "service:django"}), "not the tracker's"),
        (hand_made({**GOOD, "exp": NOW}), "expired"),
        (hand_made({**GOOD, "exp": NOW - 3600}), "expired"),
        (hand_made({**GOOD, "exp": NOW + 3600}), "too long"),
        (hand_made({**GOOD, "exp": "soon"}), "no expiry"),
        (hand_made({k: v for k, v in GOOD.items() if k != "exp"}), "no expiry"),
        (hand_made(GOOD, header=b'{"alg":"none","typ":"JWT"}'), "HS256"),
        (hand_made(GOOD, header=b'{"alg":"HS512","typ":"JWT"}'), "HS256"),
        (hand_made(GOOD).rsplit(".", 1)[0], "not a valid JWT"),
        (hand_made(GOOD) + "==", "not a valid JWT"),
        (hand_made(GOOD) + ".extra", "not a valid JWT"),
        ("not-a-token", "not a valid JWT"),
        ("", "not a valid JWT"),
    ],
)
def test_bad_tracker_service_tokens_are_refused(token, reason):
    with pytest.raises(tokens.TokenError, match=reason):
        tokens.verify_tracker_service_token(token, now=NOW)


def test_a_tampered_payload_is_refused():
    header, _payload, signature = hand_made(GOOD).split(".")
    forged = b64(json.dumps({**GOOD, "exp": EXP + 1}).encode())
    with pytest.raises(tokens.TokenError, match="signature"):
        tokens.verify_tracker_service_token(f"{header}.{forged}.{signature}", now=NOW)


def test_a_little_clock_skew_is_accepted():
    token = hand_made({**GOOD, "exp": NOW + 300 + 30})
    assert tokens.verify_tracker_service_token(token, now=NOW)["sub"] == "service:tracker"


# --- claims of the tokens Django issues (company isolation) -------------------------------------


@pytest.fixture
def two_companies(default_company):
    """Company A with two tracked sites and one untracked; company B with a tracked site."""
    settings_a = CompanySettings.load(default_company)
    settings_a.tracking_enabled = True
    settings_a.save()
    foreman = ForemanFactory()
    manager = SiteManagerFactory()
    north = SiteFactory(name="North", foremen=[foreman], manager=manager)
    south = SiteFactory(name="South", foremen=[foreman])
    off = SiteFactory(name="Off", foremen=[foreman], tracking_enabled=False)
    admin = AdminUserFactory()
    other = CompanyFactory(name="Bravo")
    with use_company(other):
        settings_b = CompanySettings.load(other)
        settings_b.tracking_enabled = True
        settings_b.save()
        bravo_site = SiteFactory(name="Bravo site")
        bravo_admin = AdminUserFactory()
    return {
        "foreman": foreman,
        "manager": manager,
        "admin": admin,
        "sites": (north, south, off),
        "bravo_site": bravo_site,
        "bravo_admin": bravo_admin,
        "bravo": other,
    }


def test_an_admin_dashboard_token_lists_only_their_company_sites(two_companies):
    north, south, _off = two_companies["sites"]
    token, _expires = services.mint_dashboard_token(two_companies["admin"])
    claims = tokens.decode(token, audience="polaris-tracker")

    assert claims["kind"] == "dashboard"
    assert claims["sub"] == f"user:{two_companies['admin'].pk}"
    assert claims["company"] == north.company_id
    assert claims["sites"] == sorted([north.pk, south.pk])
    assert two_companies["bravo_site"].pk not in claims["sites"]
    assert claims["travel"] is True

    with use_company(two_companies["bravo"]):
        bravo_token, _ = services.mint_dashboard_token(two_companies["bravo_admin"])
    bravo = tokens.decode(bravo_token, audience="polaris-tracker")
    assert bravo["sites"] == [two_companies["bravo_site"].pk]
    assert bravo["company"] == two_companies["bravo"].pk


def test_travel_is_for_company_admins_only(two_companies):
    north, *_ = two_companies["sites"]
    token, _ = services.mint_dashboard_token(two_companies["manager"])
    claims = tokens.decode(token, audience="polaris-tracker")

    assert claims["sites"] == [north.pk]
    assert claims["travel"] is False


def test_a_dashboard_token_lasts_an_hour(two_companies):
    token, expires_at = services.mint_dashboard_token(two_companies["admin"])
    claims = tokens.decode(token, audience="polaris-tracker")
    assert int(expires_at.timestamp()) == claims["exp"]


def test_a_gateway_token_names_its_gateway_and_one_site(two_companies):
    north, *_ = two_companies["sites"]
    gateway = Gateway.objects.create(site=north, label="Main gate")

    token, expires_at = services.issue_gateway_token(gateway, actor=two_companies["admin"])
    claims = tokens.decode(token, audience="polaris-tracker")

    assert claims["kind"] == "gateway"
    assert claims["sub"] == f"gateway:gw-{gateway.pk}-1"
    assert claims["company"] == north.company_id
    assert claims["sites"] == [north.pk]
    assert claims["travel"] is False
    assert (expires_at - gateway.token_issued_at).days in (364, 365)
