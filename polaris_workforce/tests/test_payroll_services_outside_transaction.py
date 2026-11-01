"""The payroll services scenarios again, without a transaction around each test, so that
refresh on read takes its no-lock first pass (see ``test_payroll_refresh_on_read``)."""

import pytest
from django.db import connection

from tests.test_payroll_services import *  # noqa: F403

pytestmark = pytest.mark.django_db(transaction=True)


def test_runs_outside_a_transaction(db):
    assert not connection.in_atomic_block
