"""The role × action table in accounts.permissions (the Users screen matrix)."""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.test import RequestFactory
from django.views import View

from apps.accounts import permissions as perms
from apps.accounts.models import User
from tests.factories import SiteFactory, UserFactory

Role = User.Role


@pytest.fixture
def world(db):
    manager = UserFactory(role=Role.SITE_MANAGER)
    foreman = UserFactory(role=Role.FOREMAN)
    mine = SiteFactory(manager=manager, foremen=[foreman])
    other = SiteFactory()
    return {
        "admin": UserFactory(role=Role.ADMIN),
        "manager": manager,
        "foreman": foreman,
        "accountant": UserFactory(role=Role.ACCOUNTANT),
        "mine": mine,
        "other": other,
    }


# (action, who) → (on own site, on another site)
EXPECTED = {
    (perms.TAKE_ROLL_CALL, "admin"): (True, True),
    (perms.TAKE_ROLL_CALL, "manager"): (True, False),
    (perms.TAKE_ROLL_CALL, "foreman"): (True, False),
    (perms.TAKE_ROLL_CALL, "accountant"): (False, False),
    (perms.APPROVE_DAYS, "manager"): (True, False),
    (perms.APPROVE_DAYS, "foreman"): (False, False),
    (perms.PAY_OUT, "foreman"): (True, False),
    (perms.GIVE_ADVANCES, "foreman"): (False, False),
    (perms.EDIT_WORKERS, "manager"): (True, False),
    (perms.LOCK_PERIODS, "admin"): (True, True),
    (perms.LOCK_PERIODS, "accountant"): (True, True),
    (perms.LOCK_PERIODS, "manager"): (True, False),  # owner decision M1: own sites
    (perms.LOCK_PERIODS, "foreman"): (False, False),
    (perms.VIEW_REPORTS, "accountant"): (True, True),
    (perms.VIEW_REPORTS, "manager"): (True, False),
    (perms.VIEW_REPORTS, "foreman"): (False, False),
    (perms.MANAGE_COMPANY, "admin"): (True, True),
    (perms.MANAGE_COMPANY, "accountant"): (False, False),
}


@pytest.mark.parametrize(("key", "expected"), EXPECTED.items())
def test_can_applies_role_and_site_scope(world, key, expected):
    action, who = key
    user = world[who]

    assert (perms.can(user, action, world["mine"]), perms.can(user, action, world["other"])) == (
        expected
    )
    assert user.can(action, world["other"]) == expected[1]


def test_own_scope_without_a_site_means_on_their_own_sites(world):
    assert perms.can(world["foreman"], perms.TAKE_ROLL_CALL)
    assert not perms.can(world["foreman"], perms.APPROVE_DAYS)


def test_anonymous_and_inactive_users_can_do_nothing(world):
    world["admin"].is_active = False
    for action in perms.PERMISSIONS:
        assert not perms.can(AnonymousUser(), action)
        assert not perms.can(world["admin"], action)


def test_superuser_counts_as_admin(world):
    root = UserFactory(role=Role.FOREMAN, is_superuser=True)

    assert perms.can(root, perms.MANAGE_COMPANY)
    assert perms.can(root, perms.APPROVE_DAYS, world["other"])


def test_unknown_action_raises(world):
    with pytest.raises(KeyError):
        perms.can(world["admin"], "fly")


def test_shortcuts(world):
    assert perms.can_lock_periods(world["accountant"], world["other"])
    assert perms.can_lock_periods(world["manager"], world["mine"])
    assert not perms.can_lock_periods(world["manager"], world["other"])
    assert not perms.can_lock_periods(world["foreman"], world["mine"])
    assert perms.can_view_reports(world["manager"], world["mine"])
    assert not perms.can_view_reports(world["manager"], world["other"])
    assert perms.can_manage_users(world["admin"])
    assert not perms.can_manage_users(world["manager"])


def test_matrix_rows_follow_the_table():
    matrix = perms.permission_matrix()

    assert [row["action"] for row in matrix] == list(perms.PERMISSIONS)
    lock = next(row for row in matrix if row["action"] == perms.LOCK_PERIODS)
    # Admin, Site manager, Foreman, Accountant
    assert lock["cells"] == [perms.ALL, perms.OWN, None, perms.ALL]
    roll_call = matrix[0]
    assert roll_call["cells"] == [perms.ALL, perms.OWN, perms.OWN, None]


class _AdminView(perms.RoleRequiredMixin, View):
    allowed_roles = (Role.ADMIN,)

    def get(self, request):
        return HttpResponse("ok")


def test_role_required_mixin(world):
    factory = RequestFactory()

    request = factory.get("/")
    request.user = AnonymousUser()
    assert _AdminView.as_view()(request).status_code == 302

    request.user = world["manager"]
    with pytest.raises(PermissionDenied):
        _AdminView.as_view()(request)

    request.user = world["admin"]
    assert _AdminView.as_view()(request).status_code == 200
