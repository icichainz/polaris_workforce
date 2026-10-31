import sqlite3

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse


def test_health_endpoint(client):
    response = client.get(reverse("api:health"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_requires_login(client):
    response = client.get(reverse("dashboard:home"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


@pytest.mark.django_db
def test_dashboard_for_signed_in_user(client):
    user = get_user_model().objects.create_user(username="edem", password="s3cret-pass")
    client.force_login(user)

    response = client.get(reverse("dashboard:home"))

    assert response.status_code == 200
    assert "Polaris Workforce" in response.content.decode()


@pytest.mark.django_db
def test_new_users_default_to_foreman_role():
    user = get_user_model().objects.create_user(username="kossi", password="s3cret-pass")

    assert user.role == get_user_model().Role.FOREMAN


def test_sqlite_init_command_enables_wal(tmp_path):
    # The test database is in memory, so check the pragmas against a real file.
    conn = sqlite3.connect(tmp_path / "check.sqlite3")
    try:
        conn.executescript(settings.SQLITE_INIT_COMMAND)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode == "wal"
    assert busy_timeout == 5000
