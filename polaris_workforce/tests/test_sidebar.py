import pytest

from apps.accounts.models import User
from tests.factories import UserFactory


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "visible"),
    [(User.Role.ADMIN, True), (User.Role.SITE_MANAGER, False), (User.Role.ACCOUNTANT, False)],
)
def test_admin_only_entries_in_sidebar(client, role, visible):
    client.force_login(UserFactory(role=role))

    html = client.get("/").content.decode()

    assert ('href="/users/"' in html) is visible
    assert ('href="/settings/"' in html) is visible
