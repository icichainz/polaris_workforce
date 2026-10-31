import datetime

import pytest

from tests.factories import PayRateFactory, WorkerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def worker_with_history():
    worker = WorkerFactory()
    rates = {
        "january": PayRateFactory(
            worker=worker, amount=3500, effective_from=datetime.date(2026, 1, 1)
        ),
        "june": PayRateFactory(
            worker=worker, amount=4500, effective_from=datetime.date(2026, 6, 1)
        ),
        "october": PayRateFactory(
            worker=worker, amount=5000, effective_from=datetime.date(2026, 10, 15)
        ),
    }
    PayRateFactory(amount=8000, effective_from=datetime.date(2026, 1, 1))  # another worker
    return worker, rates


@pytest.mark.parametrize(
    ("date", "expected"),
    [
        (datetime.date(2026, 1, 1), "january"),
        (datetime.date(2026, 5, 31), "january"),
        (datetime.date(2026, 6, 1), "june"),
        (datetime.date(2026, 10, 14), "june"),
        (datetime.date(2026, 10, 15), "october"),
        (datetime.date(2027, 3, 1), "october"),
    ],
)
def test_rate_on_returns_the_rate_in_effect(worker_with_history, date, expected):
    worker, rates = worker_with_history

    assert worker.rate_on(date) == rates[expected]


def test_rate_on_before_the_first_rate_is_none(worker_with_history):
    worker, _rates = worker_with_history

    assert worker.rate_on(datetime.date(2025, 12, 31)) is None


def test_worker_without_rates():
    assert WorkerFactory().rate_on(datetime.date(2026, 1, 1)) is None


def test_full_name_and_str():
    worker = WorkerFactory(first_name="Kossi", last_name="Agbeko", number="W-0142")

    assert worker.full_name == "Kossi Agbeko"
    assert str(worker) == "Kossi Agbeko (W-0142)"
