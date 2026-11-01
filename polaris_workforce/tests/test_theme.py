"""Colour theme (light / dark / system).

- The token split kept every light value (no visual change in light mode).
- The dark palette is generated (scripts/theme_css.py): the dark and system blocks are
  identical, cover every token, and app.css is current.
- WCAG AA contrast for the text / background pairs the templates use, in both themes.
- The preference: a signed-in user's ``User.theme``, else the ``pw_theme`` cookie, else
  light; the worker portal uses the cookie only.
- The switch endpoint (``core:set_theme``) and the switch markup.
- Every office page, sign-in page and portal page renders ``<html data-theme>``; the
  badge print sheet is always light.
"""

import importlib.util
import re
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.core import theme
from tests.factories import AdminUserFactory, CompanySettingsFactory, UserFactory
from tests.test_i18n_fr import (  # noqa: F401  (fixtures and page lists, reused)
    _office_pages,
    _wave4_pages,
    company,
    scenario,
    wave4,
)
from tests.test_payroll_services import TUE
from tests.test_portal_auth import PortalClient, signed_in

BASE = Path(django_settings.BASE_DIR)
APP_CSS = BASE / "static" / "src" / "app.css"
BUILT_CSS = BASE / "static" / "css" / "app.css"


def _load_generator():
    spec = importlib.util.spec_from_file_location("theme_css", BASE / "scripts" / "theme_css.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()
CSS = APP_CSS.read_text(encoding="utf-8")
LIGHT = GEN.light_palette(CSS)


def _block(css: str, selector: str) -> dict[str, str]:
    """The colour declarations of the first ``selector { … }`` block in ``css`` that has
    any (``[data-theme="light"]`` also opens a block with color-scheme only)."""
    declaration = re.compile(r"--color-(?P<name>[a-z0-9-]+):\s*(?P<value>#[0-9a-fA-F]{6});")
    for match in re.finditer(re.escape(selector) + r"\s*\{(?P<body>[^{}]*)\}", css):
        colours = {m["name"]: m["value"].lower() for m in declaration.finditer(match["body"])}
        if colours:
            return colours
    raise AssertionError(f"no colour block for {selector}")


def _section() -> str:
    return CSS.split(GEN.BEGIN, 1)[1].split(GEN.END, 1)[0]


def _dark() -> dict[str, str]:
    section = _section()
    return _block(section.split("@media", 1)[0], '[data-theme="dark"]')


def _system_dark() -> dict[str, str]:
    media = _section().split("@media (prefers-color-scheme: dark)", 1)[1]
    return _block(media, '[data-theme="system"]')


# --- the token split (light mode unchanged) -------------------------------------------------

# The light palette before dark mode (main at c8998f3). Every value must still be there.
LIGHT_BEFORE = {
    "white": "#ffffff",
    "ink": "#16191d",
    "ink-700": "#3f4650",
    "steel": "#5b626e",
    "line": "#e3e5e9",
    "line-soft": "#eef0f3",
    "line-strong": "#c9ced6",
    "ground": "#f4f5f7",
    "subtle": "#f8f9fa",
    "segment": "#e9ebef",
    "disabled": "#6b7280",
    "amber-500": "#f5b000",
    "amber-text": "#8a5a00",
    "amber-tint": "#fffbeb",
    "blue-700": "#1a56c4",
    "blue-800": "#123e8f",
    "present-bg": "#dcf2e4",
    "present-fg": "#135c33",
    "absent-bg": "#fbe1de",
    "absent-fg": "#9b1c13",
    "half-bg": "#fdebd2",
    "half-fg": "#8a4a06",
    "late-bg": "#dde8f8",
    "late-fg": "#1a4c99",
    "draft-bg": "#eef0f3",
    "draft-fg": "#3f4650",
    "warning-bg": "#fff4db",
    "warning-fg": "#6b4600",
    "warning-line": "#f5d78a",
    "danger-line": "#f0b8b2",
    "danger-strong": "#c0392b",
    "danger-text": "#7f1710",
    "rail-text": "#c9cdd3",
    "rail-muted": "#a3a9b2",
    "rail-label": "#969da7",
    "rail-line": "#2e343c",
    "rail-raised": "#1e2227",
    "rail-active": "#2a3038",
}

# New role token → the old token it replaced in the templates (same light value).
SPLIT = {
    "surface": "white",  # bg-white on cards, inputs, tables
    "on-inverse": "white",  # text-white on the sidebar and other dark chrome
    "on-ink": "white",  # text-white on an ink fill (selected toggle, lock, locked badge)
    "inverse": "ink",  # bg-ink of the sidebar, sign-in panel, portal bar, dark tile…
    "inverse-line": "ink",  # border-ink of the dark tile
    "on-primary": "ink",  # text-ink on amber
    "rail-edge": "ink-700",  # border-ink-700 of the outline button on the save bar
}


def test_light_values_are_unchanged():
    assert {name: LIGHT[name] for name in LIGHT_BEFORE} == LIGHT_BEFORE


def test_each_split_token_keeps_the_light_value_it_replaced():
    for new, old in SPLIT.items():
        assert LIGHT[new] == LIGHT_BEFORE[old], new


def test_segment_hover_is_white_at_60_percent_over_the_track():
    # Was hover:bg-white/60 (a color-mix, light-only in browsers without color-mix).
    track = [int(LIGHT["segment"][i : i + 2], 16) for i in (1, 3, 5)]
    blended = "#" + "".join(f"{round(0.6 * 255 + 0.4 * c):02x}" for c in track)
    assert LIGHT["segment-hover"] == blended


def test_no_new_token_besides_the_split():
    assert set(LIGHT) == set(LIGHT_BEFORE) | set(SPLIT) | {"segment-hover"}


def test_templates_keep_literal_white_for_print_only():
    templates = BASE / "templates"
    for path in templates.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        found = re.findall(r"(?<![\w-])(?:[\w-]+:)*(?:bg|text|border)-white\b", text)
        if path.name == "badge_sheet.html":
            continue
        assert not found, (path.relative_to(templates), found)


def test_text_on_amber_is_on_primary():
    """Amber keeps its value in dark mode, so its text must not follow ink."""
    for path in (BASE / "templates").rglob("*.html"):
        for classes in re.findall(r'class="([^"]*)"', path.read_text(encoding="utf-8")):
            for chunk in re.split(r"{%[^%]*%}", classes):
                tokens = chunk.split()
                if "bg-amber-500" in tokens:
                    assert "text-ink" not in tokens, (path.name, chunk)


# --- the generated dark theme -----------------------------------------------------------------


def test_app_css_generated_section_is_current():
    assert GEN.updated(CSS) == CSS, "run scripts/theme_css.py"


def test_dark_and_system_blocks_are_identical():
    assert _dark() == _system_dark()
    assert _dark() == {name: GEN.DARK[name] for name in LIGHT}


def test_every_token_has_a_dark_value():
    assert set(_dark()) == set(LIGHT)


def test_light_block_and_print_block_use_the_light_values():
    section = _section()
    assert _block(section, '[data-theme="light"]') == LIGHT
    assert _block(section.split("@media print", 1)[1], "[data-theme]") == LIGHT


def test_literal_white_and_amber_text_colour_never_change():
    dark = _dark()
    assert dark["white"] == LIGHT["white"] == "#ffffff"
    assert dark["on-primary"] == LIGHT["on-primary"]
    assert dark["amber-500"] == LIGHT["amber-500"]


def test_color_scheme_follows_the_theme():
    section = _section()
    assert re.search(r'\[data-theme="dark"\]\s*\{\s*color-scheme: dark;', section)
    assert re.search(r'\[data-theme="system"\]\s*\{\s*color-scheme: light dark;', section)
    assert re.search(
        r'prefers-color-scheme: dark\)\s*\{\s*\[data-theme="system"\]\s*\{\s*color-scheme: dark;',
        section,
    )
    assert "light-dark(" not in CSS


def test_built_css_has_the_dark_theme():
    built = BUILT_CSS.read_text(encoding="utf-8")
    assert f"--color-surface:{GEN.DARK['surface']}" in built
    assert "prefers-color-scheme:dark" in built
    assert "[data-theme=dark]" in built
    assert "aria-pressed" in built  # the switch's pressed state


# --- contrast (WCAG 2.x AA) ------------------------------------------------------------------


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# (text, background) pairs the templates use: 4.5:1 in both themes.
TEXT_PAIRS = [
    ("ink", "ground"),
    ("ink", "surface"),
    ("ink", "subtle"),
    ("ink", "segment"),
    ("ink", "segment-hover"),
    ("ink", "amber-tint"),
    ("ink", "line"),  # initials avatar
    ("ink-700", "surface"),
    ("ink-700", "ground"),
    ("ink-700", "subtle"),
    ("ink-700", "segment"),
    ("steel", "surface"),
    ("steel", "ground"),
    ("steel", "subtle"),
    ("steel", "amber-tint"),
    ("blue-700", "surface"),  # links
    ("blue-700", "ground"),
    ("blue-700", "subtle"),
    ("blue-800", "surface"),  # link hover
    ("on-primary", "amber-500"),  # primary button, active portal tab, nav badge
    ("amber-text", "surface"),
    ("amber-text", "amber-tint"),
    ("amber-text", "warning-bg"),
    ("on-ink", "ink"),  # selected toggle, lock button, locked badge
    ("on-ink", "ink-700"),  # lock button hover
    ("on-inverse", "inverse"),  # sidebar, sign-in panel, portal bar, dark tile, banner
    ("on-inverse", "rail-raised"),
    ("on-inverse", "rail-active"),
    ("on-inverse", "rail-line"),  # sidebar avatar
    ("rail-text", "inverse"),
    ("rail-text", "rail-raised"),
    ("rail-muted", "inverse"),
    ("rail-label", "inverse"),
    ("present-fg", "present-bg"),
    ("present-fg", "surface"),
    ("absent-fg", "absent-bg"),
    ("absent-fg", "surface"),  # field errors, danger button
    ("half-fg", "half-bg"),
    ("half-fg", "surface"),
    ("late-fg", "late-bg"),
    ("late-fg", "surface"),
    ("draft-fg", "draft-bg"),
    ("warning-fg", "warning-bg"),
    ("warning-fg", "surface"),
    ("danger-text", "absent-bg"),  # danger banner
]

# Non-text (3:1): bars, the field error border, the amber icon on the sidebar.
UI_PAIRS = [
    ("danger-strong", "surface"),
    ("present-fg", "line-soft"),  # progress bars
    ("blue-700", "surface"),  # chart bars
    ("amber-500", "inverse"),  # active nav icon, focus ring on the sidebar
]

# 3:1 in dark mode only. The light values are the design spec (unchanged by this
# feature) and fall short: control borders 1.6:1, the amber focus ring on white 1.9:1.
DARK_ONLY_UI_PAIRS = [
    ("line-strong", "surface"),  # input and secondary button borders
    ("amber-500", "surface"),  # focus ring
    ("amber-500", "ground"),
]


def _palette(mode: str) -> dict[str, str]:
    return LIGHT if mode == "light" else _dark()


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_contrast_is_aa(mode, fg, bg):
    palette = _palette(mode)
    assert contrast(palette[fg], palette[bg]) >= 4.5


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), UI_PAIRS)
def test_ui_contrast_is_aa(mode, fg, bg):
    palette = _palette(mode)
    assert contrast(palette[fg], palette[bg]) >= 3


@pytest.mark.parametrize(("fg", "bg"), DARK_ONLY_UI_PAIRS)
def test_dark_ui_contrast_is_aa(fg, bg):
    palette = _dark()
    assert contrast(palette[fg], palette[bg]) >= 3


def test_status_colours_stay_distinct_in_dark_mode():
    """Statuses also carry a letter and a word; their colours still differ."""
    dark = _dark()
    fgs = [dark[f"{s}-fg"] for s in ("present", "absent", "half", "late", "draft")]
    bgs = [dark[f"{s}-bg"] for s in ("present", "absent", "half", "late", "draft")]
    assert len(set(fgs)) == len(fgs)
    assert len(set(bgs)) == len(bgs)


# --- preference resolution ---------------------------------------------------------------------


@pytest.fixture
def office(db):
    CompanySettingsFactory()
    return AdminUserFactory()


def _html_theme(response) -> str:
    match = re.search(r'<html lang="[a-z]+" data-theme="([a-z]+)">', response.content.decode())
    assert match, response.content[:300]
    return match[1]


def test_default_is_light(client, office):
    assert _html_theme(client.get(reverse("login"))) == "light"
    client.force_login(office)
    assert _html_theme(client.get(reverse("dashboard:home"))) == "light"


def test_signed_out_pages_use_the_cookie(client, db):
    client.cookies[theme.COOKIE_NAME] = "dark"
    assert _html_theme(client.get(reverse("login"))) == "dark"


def test_an_unknown_cookie_value_falls_back_to_light(client, db):
    client.cookies[theme.COOKIE_NAME] = "neon"
    assert _html_theme(client.get(reverse("login"))) == "light"


def test_the_user_choice_wins_over_the_cookie(client, office):
    office.theme = "system"
    office.save(update_fields=["theme"])
    client.cookies[theme.COOKIE_NAME] = "dark"
    client.force_login(office)
    assert _html_theme(client.get(reverse("dashboard:home"))) == "system"


def test_a_user_on_light_ignores_a_dark_cookie(client, office):
    client.cookies[theme.COOKIE_NAME] = "dark"
    client.force_login(office)
    assert _html_theme(client.get(reverse("dashboard:home"))) == "light"


def test_new_users_start_light(db):
    CompanySettingsFactory()
    assert UserFactory().theme == "light"


@pytest.fixture
def portal_worker(db):
    from tests.factories import WorkerFactory
    from tests.test_portal_auth import give

    CompanySettingsFactory()
    worker = WorkerFactory(phone="+228 90 44 55 66")
    give(worker)
    return worker


def test_portal_uses_the_cookie(portal_worker):
    portal = signed_in(portal_worker)
    assert _html_theme(portal.get(reverse("portal:home"))) == "light"
    portal.client.cookies[theme.COOKIE_NAME] = "dark"
    assert _html_theme(portal.get(reverse("portal:home"))) == "dark"
    signed_out = PortalClient()
    signed_out.client.cookies[theme.COOKIE_NAME] = "dark"
    assert _html_theme(signed_out.get(reverse("portal:sign_in"))) == "dark"


def test_portal_ignores_a_signed_in_office_user(portal_worker):
    user = AdminUserFactory(theme="dark")
    portal = signed_in(portal_worker)
    portal.client.force_login(user)
    portal.client.cookies[theme.COOKIE_NAME] = "system"
    assert _html_theme(portal.get(reverse("portal:home"))) == "system"


# --- the endpoint -----------------------------------------------------------------------------


def _csrf_client() -> Client:
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("login"))
    return client


def _post(client, data, **extra):
    data = {"csrfmiddlewaretoken": client.cookies["csrftoken"].value, **data}
    return client.post(reverse("core:set_theme"), data, **extra)


def test_set_theme_needs_the_csrf_token(db):
    client = Client(enforce_csrf_checks=True)
    response = client.post(reverse("core:set_theme"), {"theme": "dark"})
    assert response.status_code == 403
    assert theme.COOKIE_NAME not in response.cookies


def test_set_theme_is_post_only(client, db):
    assert client.get(reverse("core:set_theme")).status_code == 405


@pytest.mark.parametrize("value", ["", "neon", "DARK", "light "])
def test_set_theme_refuses_an_unknown_value(db, value):
    client = _csrf_client()
    response = _post(client, {"theme": value, "next": "/"})
    assert response.status_code == 400
    assert theme.COOKIE_NAME not in response.cookies


def test_set_theme_signed_out_sets_the_cookie_and_goes_back(db):
    client = _csrf_client()
    response = _post(client, {"theme": "dark", "next": "/accounts/login/?next=/"})
    assert response.status_code == 302
    assert response["Location"] == "/accounts/login/?next=/"
    cookie = response.cookies[theme.COOKIE_NAME]
    assert cookie.value == "dark"
    assert cookie["max-age"] == 365 * 24 * 3600
    assert cookie["samesite"] == "Lax"
    assert cookie["httponly"]
    assert cookie["path"] == "/"


@pytest.mark.parametrize(
    "next_url", ["https://evil.example/", "//evil.example/", "javascript:alert(1)", ""]
)
def test_set_theme_never_redirects_off_site(db, next_url):
    client = _csrf_client()
    response = _post(client, {"theme": "light", "next": next_url})
    assert response.status_code == 302
    assert response["Location"] == "/"


def test_set_theme_falls_back_to_a_same_site_referer(db):
    client = _csrf_client()
    response = _post(client, {"theme": "light"}, HTTP_REFERER="http://testserver/portal/")
    assert response["Location"] == "http://testserver/portal/"
    response = _post(client, {"theme": "light"}, HTTP_REFERER="https://evil.example/")
    assert response["Location"] == "/"


def test_set_theme_saves_the_signed_in_user(office):
    client = _csrf_client()
    client.force_login(office)
    response = _post(client, {"theme": "system", "next": "/workers/"})
    assert response.status_code == 302
    assert response["Location"] == "/workers/"
    office.refresh_from_db()
    assert office.theme == "system"
    assert response.cookies[theme.COOKIE_NAME].value == "system"


def test_set_theme_answers_htmx_with_204(office):
    client = _csrf_client()
    client.force_login(office)
    response = _post(client, {"theme": "dark"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 204
    assert response.content == b""
    assert response.cookies[theme.COOKIE_NAME].value == "dark"
    office.refresh_from_db()
    assert office.theme == "dark"


def test_set_theme_from_the_portal_keeps_it_on_the_browser(office):
    """The portal switch (device=1) never changes an office account."""
    client = _csrf_client()
    client.force_login(office)
    response = _post(client, {"theme": "dark", "device": "1", "next": "/portal/"})
    assert response["Location"] == "/portal/"
    assert response.cookies[theme.COOKIE_NAME].value == "dark"
    office.refresh_from_db()
    assert office.theme == "light"


def test_set_theme_writes_no_audit_row(office):
    from apps.core.models import AuditLog

    client = _csrf_client()
    client.force_login(office)
    before = AuditLog.objects.count()
    _post(client, {"theme": "dark"}, HTTP_HX_REQUEST="true")
    assert AuditLog.objects.count() == before


def test_cookie_is_secure_when_session_cookies_are(db, settings):
    settings.SESSION_COOKIE_SECURE = True
    client = _csrf_client()
    response = _post(client, {"theme": "dark", "next": "/"})
    assert response.cookies[theme.COOKIE_NAME]["secure"]


def test_portal_switch_works_without_javascript(portal_worker):
    """The portal loads no JavaScript: the plain form post comes back to the page."""
    portal = signed_in(portal_worker)
    response = portal.post(
        reverse("core:set_theme"), {"theme": "dark", "device": "1", "next": "/portal/pay/"}
    )
    assert response.status_code == 302
    assert response["Location"] == "/portal/pay/"
    assert _html_theme(portal.get(reverse("portal:pay"))) == "dark"


def test_platform_admin_without_a_company_can_switch(db):
    from tests.factories import PlatformAdminFactory

    client = _csrf_client()
    client.force_login(PlatformAdminFactory())
    response = _post(client, {"theme": "dark"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 204


# --- the switch markup ---------------------------------------------------------------------------


def _switch(page: str) -> str:
    match = re.search(r'<form[^>]*action="/theme/".*?</form>', page, re.S)
    assert match, "no theme switch"
    return match.group()


def _pressed(switch: str) -> dict[str, str]:
    return dict(
        re.findall(r'value="(light|dark|system)"[^>]*?\saria-pressed="(true|false)"', switch)
    )


def test_sidebar_has_the_switch(client, office):
    office.theme = "dark"
    office.save(update_fields=["theme"])
    client.force_login(office)
    page = client.get(reverse("dashboard:home")).content.decode()
    switch = _switch(page)
    assert 'role="group"' in switch
    assert 'aria-label="Colour theme"' in switch
    assert 'hx-post="/theme/"' in switch
    assert "csrfmiddlewaretoken" in switch
    assert _pressed(switch) == {"light": "false", "dark": "true", "system": "false"}
    # Three real buttons, reachable with the keyboard. The sidebar shows icons only (it
    # must fit short laptop screens): each word is read out and shown on hover.
    assert switch.count('<button type="submit" name="theme"') == 3
    for word in ("Light", "Dark", "System"):
        assert f'<span class="sr-only">{word}</span>' in switch
    assert 'title="Light"' in switch
    assert 'title="Dark"' in switch
    assert 'title="Follow the device setting"' in switch
    assert 'name="device"' not in switch


def test_office_sign_in_has_the_switch_next_to_the_language(client, db):
    client.cookies[theme.COOKIE_NAME] = "system"
    page = client.get(reverse("login")).content.decode()
    switch = _switch(page)
    assert _pressed(switch) == {"light": "false", "dark": "false", "system": "true"}
    assert page.index('action="/theme/"') < page.index('action="/i18n/setlang/"')


def test_portal_has_the_switch_on_sign_in_and_in_the_footer(portal_worker):
    portal = PortalClient()
    for page in (
        portal.get(reverse("portal:sign_in")).content.decode(),
        signed_in(portal_worker).get(reverse("portal:home")).content.decode(),
    ):
        switch = _switch(page)
        assert 'name="device" value="1"' in switch
        assert _pressed(switch)["light"] == "true"
        assert "<script" not in page  # still no JavaScript on the portal


def test_switch_is_in_french(client, db):
    client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = "fr"
    switch = _switch(
        client.get(reverse("login"), headers={"accept-language": "fr"}).content.decode()
    )
    assert 'aria-label="Thème"' in switch
    for word in ("Clair", "Sombre", "Système"):
        assert f"<span>{word}</span>" in switch
    assert "Suivre le réglage de l’appareil" in switch


def test_theme_labels_in_french():
    with translation.override("fr"):
        from apps.core.choices import Theme

        assert [str(label) for label in Theme.labels] == ["Clair", "Sombre", "Système"]


# --- every page carries data-theme ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _today():
    with mock.patch("apps.core.dates.company_today", return_value=TUE):
        yield


PRINT_PAGES = {"checkin:badge_sheet"}


def test_every_office_page_renders_the_user_theme(client, wave4, settings):  # noqa: F811
    admin = wave4["admin"]
    admin.theme = "dark"
    admin.save(update_fields=["theme"])
    client.force_login(admin)
    pages = {**_office_pages(wave4), **_wave4_pages(wave4)}
    pages["dashboard:components"] = reverse("dashboard:components")
    pages["core:companies"] = None  # platform admins only: test_multi_company.py
    settings.DEBUG = True
    for name, url in pages.items():
        if url is None:
            continue
        response = client.get(url)
        assert response.status_code == 200, name
        expected = "light" if name in PRINT_PAGES else "dark"
        assert _html_theme(response) == expected, name
        if name not in PRINT_PAGES:
            _switch(response.content.decode())


def test_worker_portal_pages_render_the_cookie_theme(wave4):  # noqa: F811
    portal = signed_in(wave4["world"].yao)
    portal.client.cookies[theme.COOKIE_NAME] = "system"
    for name in (
        "portal:home",
        "portal:days",
        "portal:pay",
        "portal:advances",
        "portal:change_pin",
    ):
        assert _html_theme(portal.get(reverse(name))) == "system", name


def test_badge_sheet_is_always_light(client, office):
    office.theme = "dark"
    office.save(update_fields=["theme"])
    client.force_login(office)
    response = client.get(reverse("checkin:badge_sheet"))
    assert response.status_code == 200
    assert _html_theme(response) == "light"


def test_gallery_previews_both_themes(client, office, settings):
    settings.DEBUG = True
    client.force_login(office)
    page = client.get(reverse("dashboard:components")).content.decode()
    assert '<div data-theme="light"' in page
    assert '<div data-theme="dark"' in page
