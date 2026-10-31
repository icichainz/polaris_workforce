"""apps.api.idempotency: run_once, apply_changes and the response mixin."""

import uuid
from types import SimpleNamespace

import pytest
from django.db import IntegrityError

from apps.api.exceptions import Conflict
from apps.api.idempotency import (
    REPLAY_HEADER,
    IdempotentCreateMixin,
    IdempotentResult,
    apply_changes,
    client_uuid_field,
    run_once,
)


class FakeQuerySet:
    """Just enough of a queryset: ``filter(client_uuid=...).first()``."""

    def __init__(self):
        self.rows = {}

    def filter(self, client_uuid):
        return SimpleNamespace(first=lambda: self.rows.get(str(client_uuid)))

    def add(self, client_uuid, **fields):
        row = SimpleNamespace(client_uuid=str(client_uuid), **fields)
        self.rows[str(client_uuid)] = row
        return row


@pytest.mark.django_db
def test_first_call_creates_and_answers_201():
    qs, key = FakeQuerySet(), uuid.uuid4()

    result = run_once(qs, key, create=lambda: qs.add(key, pay_line_id=1))

    assert result.created is True
    assert result.status_code == 201
    assert result.instance.pay_line_id == 1


@pytest.mark.django_db
def test_replay_returns_the_original_without_creating():
    qs, key = FakeQuerySet(), uuid.uuid4()
    original = qs.add(key, pay_line_id=1)

    def create():
        raise AssertionError("must not create twice")

    result = run_once(qs, key, create=create, matches=lambda p: p.pay_line_id == 1)

    assert result.created is False
    assert result.status_code == 200
    assert result.instance is original


@pytest.mark.django_db
def test_reusing_a_client_uuid_for_another_request_is_a_conflict():
    qs, key = FakeQuerySet(), uuid.uuid4()
    qs.add(key, pay_line_id=1)

    with pytest.raises(Conflict) as excinfo:
        run_once(qs, key, create=lambda: None, matches=lambda p: p.pay_line_id == 2)

    assert excinfo.value.details == {"client_uuid": str(key)}


@pytest.mark.django_db
def test_race_on_the_unique_constraint_returns_the_winner():
    qs, key = FakeQuerySet(), uuid.uuid4()

    def create():
        qs.add(key, pay_line_id=1)  # the other request committed first
        raise IntegrityError("UNIQUE constraint failed: client_uuid")

    result = run_once(qs, key, create=create)

    assert result.created is False
    assert result.instance.pay_line_id == 1


@pytest.mark.django_db
def test_other_integrity_errors_are_not_swallowed():
    qs = FakeQuerySet()

    def create():
        raise IntegrityError("UNIQUE constraint failed: day, worker")

    with pytest.raises(IntegrityError):
        run_once(qs, uuid.uuid4(), create=create)


def test_apply_changes_reports_only_changed_fields():
    record = SimpleNamespace(status="present", arrival="07:00", note="")

    changed = apply_changes(record, {"status": "late", "arrival": "07:00", "note": ""})

    assert changed == ["status"]
    assert record.status == "late"
    assert apply_changes(record, {"status": "late"}) == []


def test_apply_changes_limited_to_fields():
    record = SimpleNamespace(status="present", note="")

    assert apply_changes(record, {"status": "absent", "note": "x"}, fields=["note"]) == ["note"]
    assert record.status == "present"


def test_mixin_marks_replays():
    view = IdempotentCreateMixin()

    created = view.idempotent_response(IdempotentResult(object(), True), {"id": 1})
    replayed = view.idempotent_response(IdempotentResult(object(), False), {"id": 1})

    assert created.status_code == 201
    assert REPLAY_HEADER not in created
    assert replayed.status_code == 200
    assert replayed[REPLAY_HEADER] == "true"


def test_client_uuid_field_validates_uuids():
    field = client_uuid_field()

    assert field.run_validation("6f1c2b9e-1d1a-4c55-9a52-3f0e8a9d7b10")
    assert field.help_text
