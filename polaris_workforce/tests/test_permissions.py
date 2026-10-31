import pytest
from django.core.exceptions import PermissionDenied

from apps.accounts.models import User
from apps.accounts.permissions import (
    can_manage_site,
    can_view_site,
    get_site_or_403,
    has_role,
    scoped_sites,
)
from tests.factories import SiteFactory, UserFactory

Role = User.Role


@pytest.fixture
def sites(db):
    manager = UserFactory(role=Role.SITE_MANAGER)
    foreman = UserFactory(role=Role.FOREMAN)
    mine = SiteFactory(manager=manager)
    mine.foremen.add(foreman)
    other = SiteFactory()
    return {"manager": manager, "foreman": foreman, "mine": mine, "other": other}


@pytest.mark.parametrize("role", [Role.ADMIN, Role.ACCOUNTANT])
def test_admin_and_accountant_see_every_site(sites, role):
    user = UserFactory(role=role)

    assert set(scoped_sites(user)) == {sites["mine"], sites["other"]}


@pytest.mark.parametrize("who", ["manager", "foreman"])
def test_managers_and_foremen_only_see_their_sites(sites, who):
    user = sites[who]

    assert list(scoped_sites(user)) == [sites["mine"]]
    assert can_view_site(user, sites["mine"])
    assert not can_view_site(user, sites["other"])


def test_only_admins_and_own_managers_can_manage(sites):
    admin = UserFactory(role=Role.ADMIN)

    assert can_manage_site(admin, sites["other"])
    assert can_manage_site(sites["manager"], sites["mine"])
    assert not can_manage_site(sites["manager"], sites["other"])
    assert not can_manage_site(sites["foreman"], sites["mine"])
    assert not can_manage_site(UserFactory(role=Role.ACCOUNTANT), sites["mine"])


def test_get_site_or_403_hides_sites_outside_scope(sites):
    assert get_site_or_403(sites["foreman"], sites["mine"].pk) == sites["mine"]
    with pytest.raises(PermissionDenied):
        get_site_or_403(sites["foreman"], sites["other"].pk)
    with pytest.raises(PermissionDenied):
        get_site_or_403(sites["foreman"], 999_999)


@pytest.mark.django_db
def test_inactive_users_have_no_role_and_no_sites():
    user = UserFactory(role=Role.ADMIN, is_active=False)

    assert not has_role(user, Role.ADMIN)
    assert not scoped_sites(user).exists()


@pytest.mark.django_db
def test_superuser_counts_as_admin():
    user = UserFactory(role=Role.FOREMAN, is_superuser=True)
    site = SiteFactory()

    assert has_role(user, Role.ADMIN)
    assert list(scoped_sites(user)) == [site]
