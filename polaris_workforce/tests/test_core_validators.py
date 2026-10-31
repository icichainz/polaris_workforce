import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.validators import (
    MAX_UPLOAD_SIZE,
    validate_file_size,
    validate_time_zone,
    validate_upload_extension,
)
from apps.payroll.models import Payment
from apps.workers.models import Worker


@pytest.mark.parametrize("name", ["proof.jpg", "proof.JPEG", "proof.png", "proof.webp", "id.pdf"])
def test_accepts_allowed_extensions(name):
    validate_upload_extension(SimpleUploadedFile(name, b"x"))


@pytest.mark.parametrize("name", ["proof.gif", "proof.exe", "proof.svg", "proof"])
def test_rejects_other_extensions(name):
    with pytest.raises(ValidationError):
        validate_upload_extension(SimpleUploadedFile(name, b"x"))


def test_accepts_a_file_of_exactly_5_mb():
    validate_file_size(SimpleUploadedFile("big.jpg", b"0" * MAX_UPLOAD_SIZE))


def test_rejects_a_file_over_5_mb():
    with pytest.raises(ValidationError) as exc:
        validate_file_size(SimpleUploadedFile("big.jpg", b"0" * (MAX_UPLOAD_SIZE + 1)))
    assert exc.value.code == "file_too_large"


def test_upload_fields_use_the_validators():
    for field in (Worker._meta.get_field("photo"), Payment._meta.get_field("proof_file")):
        assert validate_file_size in field.validators
        assert validate_upload_extension in field.validators


def test_time_zone_validator():
    validate_time_zone("Africa/Lome")
    with pytest.raises(ValidationError):
        validate_time_zone("Nowhere/Town")
