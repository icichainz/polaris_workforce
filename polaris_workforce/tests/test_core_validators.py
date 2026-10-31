import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.validators import (
    MAX_UPLOAD_SIZE,
    validate_file_content,
    validate_file_size,
    validate_time_zone,
    validate_upload_extension,
)
from apps.payroll.models import Payment
from apps.workers.models import Worker

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"0" * 32
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"0" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"0" * 32
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 32
HTML = b"<!doctype html><script>alert(1)</script>"

GOOD_FILES = [
    ("proof.jpg", JPEG),
    ("proof.JPEG", JPEG),
    ("proof.png", PNG),
    ("proof.webp", WEBP),
    ("id.pdf", PDF),
]


@pytest.mark.parametrize(("name", "content"), GOOD_FILES)
def test_accepts_allowed_extensions(name, content):
    upload = SimpleUploadedFile(name, content)
    validate_upload_extension(upload)
    validate_file_content(upload)
    assert upload.read() == content  # the file is rewound for the storage


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("x.jpg", HTML),  # an HTML page renamed
        ("x.png", JPEG),  # the right family, the wrong type
        ("x.webp", b"RIFF\x24\x00\x00\x00WAVEfmt "),  # a RIFF file that is not WebP
        ("x.pdf", PNG),
        ("x.jpeg", b""),  # empty
        ("x.jpg", b"\xff\xd8"),  # truncated header
    ],
)
def test_rejects_content_that_does_not_match_the_extension(name, content):
    with pytest.raises(ValidationError) as exc:
        validate_upload_extension(SimpleUploadedFile(name, content))
    assert exc.value.code == "invalid_file_content"


def test_stored_files_are_not_read_again(tmp_path, settings):
    # Re-validating a saved model must not open (or fail on) a file already on disk.
    settings.MEDIA_ROOT = tmp_path
    worker = Worker(first_name="A", last_name="B")
    worker.photo.save("old.jpg", ContentFile(b"not checked again"), save=False)
    validate_upload_extension(worker.photo)


def test_content_check_needs_no_migration():
    # Deconstructs as Django's FileExtensionValidator, so the fields are unchanged.
    path, args, kwargs = validate_upload_extension.deconstruct()
    assert path == "django.core.validators.FileExtensionValidator"
    assert kwargs == {"allowed_extensions": ["jpg", "jpeg", "png", "webp", "pdf"]}


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
