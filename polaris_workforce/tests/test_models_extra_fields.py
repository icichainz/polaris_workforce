import datetime

import pytest

from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from tests.factories import SiteFactory, UserFactory


@pytest.mark.django_db
def test_user_language_is_blank_by_default_meaning_company_language():
    user = UserFactory()

    assert user.language == ""


@pytest.mark.django_db
def test_roll_call_due_falls_back_to_company_setting():
    company = CompanySettings.load()
    site = SiteFactory(roll_call_due=None)

    assert resolve_setting("roll_call_due", site=site) == company.roll_call_due


@pytest.mark.django_db
def test_site_can_override_roll_call_due():
    site = SiteFactory(roll_call_due=datetime.time(7, 30))

    assert resolve_setting("roll_call_due", site=site) == datetime.time(7, 30)
