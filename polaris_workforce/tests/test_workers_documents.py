"""Worker documents: upload, list, download, delete, expiry warnings, scoping."""

import datetime
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.workers.models import WorkerDocument
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
    WorkerDocumentFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 31)
PDF = b"%PDF-1.7\n" + b"0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


@pytest.fixture(autouse=True)
def _setup(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    with mock.patch("apps.core.dates.company_today", return_value=TODAY):
        yield


@pytest.fixture
def world(db):
    CompanySettingsFactory()
    manager = SiteManagerFactory()
    foreman = ForemanFactory()
    site = SiteFactory(name="Riverside", manager=manager, foremen=[foreman])
    worker = WorkerFactory(first_name="Yao", last_name="Mensah")
    SiteAssignmentFactory(site=site, worker=worker, start_date=datetime.date(2026, 3, 3))
    other_manager = SiteManagerFactory()
    SiteFactory(name="Route 4 Bridge", manager=other_manager)
    return {
        "worker": worker,
        "manager": manager,
        "foreman": foreman,
        "other_manager": other_manager,
        "admin": AdminUserFactory(),
        "accountant": UserFactory(role="accountant"),
    }


def stored(worker, title="Contract", content=PDF, name="contract.pdf", **fields):
    document = WorkerDocument(worker=worker, type="contract", title=title, **fields)
    document.file.save(name, ContentFile(content), save=True)
    return document


def upload(office, worker, **extra):
    data = {
        "type": "id_scan",
        "title": "National ID (front)",
        "issued_on": "2024-01-10",
        "expires_on": "2029-01-09",
        "file": SimpleUploadedFile("scan.pdf", PDF, content_type="application/pdf"),
    }
    data.update(extra)
    return office.post(reverse("workers:document_upload", args=[worker.pk]), data)


# --- upload ------------------------------------------------------------------------------


def test_site_manager_uploads_a_document(world):
    office = Office(world["manager"])

    response = upload(office, world["worker"])

    assert response.status_code == 302
    assert response["Location"].endswith("#documents")
    document = WorkerDocument.objects.get()
    assert (document.type, document.title) == ("id_scan", "National ID (front)")
    assert document.expires_on == datetime.date(2029, 1, 9)
    assert document.uploaded_by == world["manager"]
    assert document.file.name.startswith("worker-documents/")
    assert "scan" not in document.file.name  # random name
    entry = AuditLog.objects.get(action="workers.document_add")
    assert entry.after["title"] == "National ID (front)"
    assert entry.object_id == str(world["worker"].pk)


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("page.html", b"<html></html>", "not allowed"),
        ("scan.pdf", JPEG, "does not match its type"),
        ("photo.jpg", b"<svg></svg>", "does not match its type"),
    ],
)
def test_bad_files_are_refused(world, name, content, message):
    office = Office(world["manager"])

    response = upload(office, world["worker"], file=SimpleUploadedFile(name, content))

    assert response.status_code == 400
    assert message in " ".join(response.context["document_form"].errors["file"])
    assert not WorkerDocument.objects.exists()
    assert response.context["show_document_form"] is True


def test_too_large_files_are_refused(world, monkeypatch):
    monkeypatch.setattr("apps.core.validators.MAX_UPLOAD_SIZE", 32)
    office = Office(world["manager"])

    response = upload(office, world["worker"])

    assert response.status_code == 400
    assert "too large" in response.context["document_form"].errors["file"][0]
    assert not WorkerDocument.objects.exists()


def test_expiry_before_issue_is_refused(world):
    response = upload(Office(world["manager"]), world["worker"], expires_on="2020-01-01")

    assert response.status_code == 400
    assert response.context["document_form"].errors["expires_on"]


def test_title_and_file_are_required(world):
    response = upload(Office(world["manager"]), world["worker"], title="", file="")

    errors = response.context["document_form"].errors
    assert "title" in errors and "file" in errors


# --- list and download --------------------------------------------------------------------


def test_profile_lists_documents_with_protected_links(world):
    document = stored(world["worker"], title="Signed contract")
    office = Office(world["manager"])

    html = office.get(reverse("workers:detail", args=[world["worker"].pk])).content.decode()

    assert "Signed contract" in html
    url = reverse("workers:document_file", args=[document.pk])
    assert url in html
    assert document.file.name not in html  # the stored name is never shown

    response = office.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert b"".join(response.streaming_content) == PDF


def test_image_documents_open_inline(world):
    document = stored(world["worker"], content=JPEG, name="id.jpg")

    response = Office(world["admin"]).get(reverse("workers:document_file", args=[document.pk]))

    assert response["Content-Type"] == "image/jpeg"
    assert response["Content-Disposition"].startswith("inline")


def test_accountant_reads_but_cannot_change(world):
    document = stored(world["worker"])
    office = Office(world["accountant"])

    page = office.get(reverse("workers:detail", args=[world["worker"].pk]))

    assert page.context["documents"][0]["document"] == document
    assert page.context["document_form"] is None
    assert office.get(reverse("workers:document_file", args=[document.pk])).status_code == 200
    assert upload(office, world["worker"]).status_code == 403
    delete = reverse("workers:document_delete", args=[world["worker"].pk, document.pk])
    assert office.post(delete).status_code == 403
    assert WorkerDocument.objects.count() == 1


def test_foremen_never_see_documents(world):
    document = stored(world["worker"], title="Signed contract")
    office = Office(world["foreman"])

    page = office.get(reverse("workers:detail", args=[world["worker"].pk]))

    assert page.status_code == 200
    assert "Signed contract" not in page.content.decode()
    assert office.get(reverse("workers:document_file", args=[document.pk])).status_code == 404
    assert upload(office, world["worker"]).status_code == 403


def test_another_sites_manager_cannot_reach_the_documents(world):
    document = stored(world["worker"])
    office = Office(world["other_manager"])
    worker = world["worker"]

    assert office.get(reverse("workers:detail", args=[worker.pk])).status_code == 403
    assert office.get(reverse("workers:document_file", args=[document.pk])).status_code == 404
    assert upload(office, worker).status_code == 403
    delete = reverse("workers:document_delete", args=[worker.pk, document.pk])
    assert office.post(delete).status_code == 403
    assert WorkerDocument.objects.count() == 1


def test_a_document_cannot_be_deleted_through_another_worker(world):
    document = stored(world["worker"])
    other = WorkerFactory()
    SiteAssignmentFactory(
        site=world["worker"].assignments.get().site, worker=other, start_date=TODAY
    )

    response = Office(world["manager"]).post(
        reverse("workers:document_delete", args=[other.pk, document.pk])
    )

    assert response.status_code == 404
    assert WorkerDocument.objects.filter(pk=document.pk).exists()


def test_other_companies_get_404_for_files_and_media_names(world):
    document = stored(world["worker"])
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        other_admin = AdminUserFactory()
    office = Office(other_admin)

    assert office.get(reverse("workers:document_file", args=[document.pk])).status_code == 404
    assert office.get(document.file.url).status_code == 404  # /files/media/<name>
    assert Office(world["admin"]).get(document.file.url).status_code == 200


# --- delete -------------------------------------------------------------------------------


def test_deleting_removes_the_row_and_the_file(world, django_capture_on_commit_callbacks):
    document = stored(world["worker"], title="Old contract")
    name = document.file.name
    assert default_storage.exists(name)
    office = Office(world["manager"])

    with django_capture_on_commit_callbacks(execute=True):
        response = office.post(
            reverse("workers:document_delete", args=[world["worker"].pk, document.pk])
        )

    assert response.status_code == 302
    assert not WorkerDocument.objects.exists()
    assert not default_storage.exists(name)
    entry = AuditLog.objects.get(action="workers.document_delete")
    assert entry.before["title"] == "Old contract"


def test_delete_needs_post(world):
    document = stored(world["worker"])
    url = reverse("workers:document_delete", args=[world["worker"].pk, document.pk])

    assert Office(world["manager"]).get(url).status_code == 405


# --- expiry -------------------------------------------------------------------------------


def test_expiry_status():
    document = WorkerDocument(expires_on=None)
    assert document.expiry(TODAY) is None
    document.expires_on = TODAY - datetime.timedelta(days=1)
    assert document.expiry(TODAY) == "expired"
    document.expires_on = TODAY
    assert document.expiry(TODAY) == "expiring"
    document.expires_on = TODAY + datetime.timedelta(days=30)
    assert document.expiry(TODAY) == "expiring"
    document.expires_on = TODAY + datetime.timedelta(days=31)
    assert document.expiry(TODAY) is None


def test_profile_warns_about_expired_and_expiring_documents(world):
    worker = world["worker"]
    WorkerDocumentFactory(worker=worker, title="Old ID", expires_on=TODAY - datetime.timedelta(1))
    WorkerDocumentFactory(worker=worker, title="Permit", expires_on=TODAY + datetime.timedelta(9))
    WorkerDocumentFactory(
        worker=worker, title="Contract", expires_on=TODAY + datetime.timedelta(90)
    )

    response = Office(world["manager"]).get(reverse("workers:detail", args=[worker.pk]))

    assert (response.context["expired_count"], response.context["expiring_count"]) == (1, 1)
    html = response.content.decode()
    assert "1 document has expired." in html
    assert "1 document expires within 30 days." in html
    expiry = {d["document"].title: d["expiry"] for d in response.context["documents"]}
    assert expiry == {"Old ID": "expired", "Permit": "expiring", "Contract": None}


def test_workers_list_filters_and_flags_documents(world):
    site = world["worker"].assignments.get().site
    expired, expiring, fine = world["worker"], WorkerFactory(), WorkerFactory()
    for worker in (expiring, fine):
        SiteAssignmentFactory(site=site, worker=worker, start_date=TODAY)
    WorkerDocumentFactory(worker=expired, expires_on=TODAY - datetime.timedelta(days=3))
    WorkerDocumentFactory(worker=expired, expires_on=TODAY + datetime.timedelta(days=3))
    WorkerDocumentFactory(worker=expiring, expires_on=TODAY + datetime.timedelta(days=20))
    WorkerDocumentFactory(worker=fine, expires_on=TODAY + datetime.timedelta(days=200))
    office = Office(world["manager"])
    url = reverse("workers:list")

    def listed(**query):
        response = office.get(url, query)
        return {row["worker"]: row["documents"] for row in response.context["rows"]}

    assert listed() == {expired: "expired", expiring: "expiring", fine: None}
    assert listed(documents="attention") == {expired: "expired", expiring: "expiring"}
    assert listed(documents="expired") == {expired: "expired"}
    assert "Document expired" in office.get(url).content.decode()


def test_foremen_get_no_document_filter_or_flags(world):
    WorkerDocumentFactory(worker=world["worker"], expires_on=TODAY - datetime.timedelta(days=3))
    office = Office(world["foreman"])

    response = office.get(reverse("workers:list"), {"documents": "expired"})

    assert "documents" not in response.context["form"].fields
    assert [row["documents"] for row in response.context["rows"]] == [None]
