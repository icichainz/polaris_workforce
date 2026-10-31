"""The OpenAPI contract: served, valid, complete, and identical to docs/api/openapi.yaml."""

from pathlib import Path

import pytest
import yaml
from django.conf import settings
from django.core.management import call_command
from django.urls import reverse
from drf_spectacular.validation import validate_schema
from rest_framework.test import APIClient

CONTRACT_FILE = Path(settings.BASE_DIR).parent / "docs" / "api" / "openapi.yaml"

EXPECTED_OPERATIONS = {
    ("get", "/api/v1/health/"),
    ("post", "/api/v1/auth/pin/"),
    ("post", "/api/v1/auth/logout/"),
    ("get", "/api/v1/me/"),
    ("get", "/api/v1/sites/{site_id}/crew/"),
    ("get", "/api/v1/sites/{site_id}/days/"),
    ("get", "/api/v1/sites/{site_id}/days/{date}/"),
    ("put", "/api/v1/sites/{site_id}/days/{date}/records/"),
    ("post", "/api/v1/sites/{site_id}/days/{date}/submit/"),
    ("post", "/api/v1/records/{record_id}/corrections/"),
    ("get", "/api/v1/sync/"),
    ("get", "/api/v1/sites/{site_id}/payouts/{date}/"),
    ("post", "/api/v1/payments/"),
}


@pytest.fixture(scope="module")
def schema():
    response = APIClient().get(reverse("api:schema"), {"format": "json"})
    assert response.status_code == 200
    return response.json()


def _operations(schema):
    return {
        (method, path)
        for path, item in schema["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def test_health_endpoint_still_works(client):
    response = client.get("/api/v1/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_schema_is_valid_openapi(schema):
    validate_schema(schema)
    assert schema["info"]["title"] == "Polaris Workforce API"
    assert schema["info"]["version"] == "1.1.0"


def test_schema_has_every_foreman_endpoint(schema):
    assert _operations(schema) == EXPECTED_OPERATIONS


def test_every_operation_is_documented(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            assert operation.get("summary"), f"{method} {path} has no summary"
            assert operation.get("description"), f"{method} {path} has no description"
            assert operation.get("tags"), f"{method} {path} has no tag"


def test_errors_use_the_shared_error_component(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            for status_code, response in operation["responses"].items():
                if int(status_code) < 400:
                    continue
                ref = response["content"]["application/json"]["schema"]["$ref"]
                assert ref == "#/components/schemas/Error", f"{method} {path} {status_code}"


def test_device_token_security_scheme(schema):
    scheme = schema["components"]["securitySchemes"]["DeviceToken"]
    assert scheme == {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": scheme["description"],
    }
    me = schema["paths"]["/api/v1/me/"]["get"]
    assert {"DeviceToken": []} in me["security"]
    # [{}] is OpenAPI for "no authentication required".
    assert schema["paths"]["/api/v1/auth/pin/"]["post"]["security"] == [{}]


def test_money_fields_are_integers(schema):
    payout = schema["components"]["schemas"]["Payout"]["properties"]
    for name in ("cash_issued", "total_to_pay", "paid_total", "left_to_pay", "cash_in_hand"):
        assert payout[name]["type"] == "integer", name
    assert "currency" in payout


def test_contract_file_matches_generated_schema(tmp_path):
    """docs/api/openapi.yaml must be regenerated whenever the API changes (see its README)."""
    generated = tmp_path / "openapi.yaml"
    call_command("spectacular", "--file", str(generated), "--validate", "--fail-on-warn")

    assert CONTRACT_FILE.exists(), "docs/api/openapi.yaml is missing; see docs/api/README.md"
    assert yaml.safe_load(CONTRACT_FILE.read_text()) == yaml.safe_load(generated.read_text()), (
        "docs/api/openapi.yaml is out of date. Regenerate it from polaris_workforce/: "
        "python manage.py spectacular --file ../docs/api/openapi.yaml --validate"
    )


def test_swagger_ui_only_in_debug():
    from apps.api import urls

    names = {pattern.name for pattern in urls.urlpatterns if hasattr(pattern, "name")}
    assert "schema" in names
    assert ("docs" in names) is settings.DEBUG
