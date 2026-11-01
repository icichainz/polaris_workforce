"""Finding workers: ``Worker.objects.search`` and ``Worker.search_text``."""

import pytest
from django.urls import reverse

from apps.workers.models import Worker
from apps.workers.search import fold, search_terms
from tests.factories import AdminUserFactory, WorkerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def elodie():
    return WorkerFactory(
        first_name="Élodie", last_name="Ékué-Mensah", number="W-0142", phone="+228 90 12 34 56"
    )


@pytest.fixture
def others():
    return [
        WorkerFactory(
            first_name="Yao", last_name="Agbeko", number="W-0007", phone="+228 99 88 77 66"
        ),
        WorkerFactory(first_name="Elom", last_name="Dossou", number="W-0300", phone=""),
    ]


def found(text):
    return list(Worker.objects.search(text))


@pytest.mark.parametrize("text", ["élodie", "ELODIE", "elodie", "Ékué", "ekue-mensah", "lodi"])
def test_names_are_found_whatever_the_case_and_the_accents(elodie, others, text):
    assert found(text) == [elodie]


def test_every_word_must_match(elodie, others):
    assert found("elodie mensah") == [elodie]
    assert found("elodie dossou") == []
    assert set(found("el")) == {elodie, others[1]}


@pytest.mark.parametrize("text", ["90 12 34", "90-12-34", "+22890", "90.12", "(228) 90"])
def test_phones_are_found_as_typed(elodie, others, text):
    assert found(text) == [elodie]


@pytest.mark.parametrize("text", ["W-0142", "w-0142", "0142"])
def test_worker_numbers(elodie, others, text):
    assert found(text) == [elodie]


def test_an_empty_search_finds_everyone(elodie, others):
    assert set(found("  ")) == {elodie, *others}


def test_saving_keeps_the_search_text_current(elodie):
    elodie.last_name = "Amouzou"
    elodie.save(update_fields=["last_name"])
    elodie.phone = "+228 70 00 00 01"
    elodie.save()

    assert found("amouzou") == [elodie]
    assert found("mensah") == []
    assert found("70 00") == [elodie]
    elodie.refresh_from_db()
    assert elodie.search_text == "elodie amouzou w-0142 +22870000001"


def test_search_terms():
    assert search_terms("  Élodie   Ékué ") == ["elodie", "ekue"]
    assert search_terms("+228 90 12") == ["+2289012"]
    assert search_terms("W-0142 90-12") == ["w-0142", "9012"]
    assert fold("ÇA Œuvre") == "ca œuvre"


def test_the_workers_list_finds_accented_names(client, elodie, others):
    client.force_login(AdminUserFactory())

    html = client.get(reverse("workers:list"), {"q": "elodie ekue"}).content.decode()

    assert "Ékué-Mensah" in html and "Agbeko" not in html
