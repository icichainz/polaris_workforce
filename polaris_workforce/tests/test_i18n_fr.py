"""French translation: the catalogue is complete, every office page renders in French,
status letters follow the language (late is R for "retard"), dates use French names, and
API error messages follow ``Accept-Language``."""

import datetime
import gettext
import html as html_lib
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings as django_settings
from django.template import Context, Template
from django.urls import reverse
from django.utils import translation

from apps.core.choices import status_letter
from apps.payroll import labels, services
from apps.payroll.models import PayPeriod
from tests.factories import (
    AdminUserFactory,
    AdvanceFactory,
    AuditLogFactory,
    CashIssueFactory,
    CompanySettingsFactory,
)
from tests.test_payroll_services import MON, SUBMITTED, TUE, hire, line_of, roll_call, setup_site

LOCALE_DIR = Path(django_settings.BASE_DIR) / "locale" / "fr" / "LC_MESSAGES"
PO_FILE = LOCALE_DIR / "django.po"
MO_FILE = LOCALE_DIR / "django.mo"
FR = {"accept-language": "fr"}


def _pick_language(client, language="fr"):
    """Signed in, the office follows the switcher cookie (not Accept-Language)."""
    client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = language


# --- the catalogue -------------------------------------------------------------------------


def _msgfmt():
    path = shutil.which("msgfmt") or next(
        (p for p in ("/opt/homebrew/bin/msgfmt", "/usr/local/bin/msgfmt") if Path(p).exists()),
        None,
    )
    if path is None:
        pytest.skip("GNU gettext (msgfmt) is not installed")
    return path


def test_catalogue_has_no_untranslated_or_fuzzy_entries(tmp_path):
    result = subprocess.run(
        [_msgfmt(), "--check", "--statistics", "-o", str(tmp_path / "check.mo"), str(PO_FILE)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stats = result.stderr.strip()
    assert "translated message" in stats
    assert "untranslated" not in stats, stats
    assert "fuzzy" not in stats, stats


def test_compiled_catalogue_matches_the_po_file(tmp_path):
    """django.mo is committed: it must be compiled from the current django.po."""
    fresh = tmp_path / "fresh.mo"
    subprocess.run([_msgfmt(), "-o", str(fresh), str(PO_FILE)], check=True)

    with fresh.open("rb") as a, MO_FILE.open("rb") as b:
        assert gettext.GNUTranslations(a)._catalog == gettext.GNUTranslations(b)._catalog


def test_the_language_switch_offers_french():
    assert ("fr", "French") in [(code, str(name)) for code, name in django_settings.LANGUAGES]


# --- a realistic data set -------------------------------------------------------------------


@pytest.fixture
def company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return CompanySettingsFactory()


@pytest.fixture
def scenario(company):
    """Monday approved, paid in part; Tuesday submitted and waiting for approval."""
    site, manager, foreman = setup_site(name="Chantier Lomé Port")
    yao = hire(site, 6000, first_name="Yao", last_name="Mensah")
    afi = hire(site, 3500, first_name="Afi", last_name="Dosseh")
    kofi = hire(site, 5000, first_name="Kofi", last_name="Agbeko")
    ama = hire(site, 4000, first_name="Ama", last_name="Kudjo")
    AdvanceFactory(
        worker=yao,
        amount=10000,
        balance=10000,
        deduction_per_pay=2000,
        given_on=MON - datetime.timedelta(days=3),
    )
    roll_call(site, MON, {yao: "present", afi: "half_day", kofi: "late", ama: "absent"})
    services.cash_sheet(site, MON)
    CashIssueFactory(site=site, amount=50_000)
    line = line_of(site, MON, yao)
    services.record_payment(foreman, line, amount=line.net, proof_type="thumb_mark")
    roll_call(site, TUE, {yao: "present", afi: "present", kofi: "late", ama: "absent"}, SUBMITTED)
    admin = AdminUserFactory(first_name="Edem", last_name="Kpodar")
    AuditLogFactory(actor=admin)
    return {"site": site, "manager": manager, "foreman": foreman, "yao": yao, "admin": admin}


@pytest.fixture(autouse=True)
def _fixed_today():
    with mock.patch("apps.core.dates.company_today", return_value=TUE):
        yield


def _office_pages(s):
    site, yao, foreman = s["site"], s["yao"], s["foreman"]
    period = PayPeriod.objects.filter(site=site).first()
    return {
        "dashboard:home": reverse("dashboard:home"),
        "dashboard:reports": reverse("dashboard:reports")
        + f"?period=custom&start={MON.isoformat()}&end={TUE.isoformat()}",
        "core:settings": reverse("core:settings"),
        "core:audit_log": reverse("core:audit_log"),
        "attendance:approvals": reverse("attendance:approvals"),
        "payroll:cash_sheets": reverse("payroll:cash_sheets"),
        "payroll:cash_sheet": reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()]),
        "payroll:periods": reverse("payroll:periods") + ("?tab=all" if period else ""),
        "payroll:advances": reverse("payroll:advances"),
        "workers:list": reverse("workers:list"),
        "workers:detail": reverse("workers:detail", args=[yao.pk]) + "?month=2025-09",
        "workers:create": reverse("workers:create"),
        "workers:edit": reverse("workers:edit", args=[yao.pk]),
        "worksites:list": reverse("worksites:list"),
        "worksites:create": reverse("worksites:create"),
        "worksites:detail": reverse("worksites:detail", args=[site.pk]),
        "worksites:crew": reverse("worksites:crew", args=[site.pk]),
        "worksites:attendance": reverse("worksites:attendance", args=[site.pk]),
        "worksites:pay": reverse("worksites:pay", args=[site.pk]),
        "worksites:settings": reverse("worksites:settings", args=[site.pk]),
        "worksites:assign": reverse("worksites:assign", args=[site.pk]),
        "worksites:edit": reverse("worksites:edit", args=[site.pk]),
        "accounts:users": reverse("accounts:users"),
        "accounts:user_create": reverse("accounts:user_create"),
        "accounts:user_edit": reverse("accounts:user_edit", args=[foreman.pk]),
    }


def _visible_text(page: str) -> str:
    page = re.sub(r"<script\b.*?</script>", " ", page, flags=re.S)
    page = re.sub(r"<style\b.*?</style>", " ", page, flags=re.S)
    attrs = " ".join(re.findall(r'(?:title|aria-label|placeholder|alt)="([^"]*)"', page))
    text = re.sub(r"<[^>]+>", " ", page)
    # Collapse ASCII whitespace only: French keeps its no-break spaces before "?" and ":".
    return re.sub(r"[ \t\r\n]+", " ", html_lib.unescape(f"{text} {attrs}"))


def _english_phrases() -> list[str]:
    """Msgids of two words or more whose French differs: none may show on a French page."""
    with MO_FILE.open("rb") as f:
        catalog = gettext.GNUTranslations(f)._catalog
    phrases = set()
    for key, value in catalog.items():
        msgid = key[0] if isinstance(key, tuple) else key
        if not msgid or "\x04" in msgid or "%" in msgid or "<" in msgid:
            continue
        if len(msgid.split()) >= 2 and value != msgid and any(c.isalpha() for c in msgid):
            phrases.add(msgid)
    return sorted(phrases, key=len, reverse=True)


# Per page: phrases that must show in French (beyond the shared sidebar).
EXPECTED_FR = {
    "dashboard:home": ["Aujourd’hui sur les chantiers", "Chiffres clés"],
    "dashboard:reports": ["Taux de présence", "Présences par ouvrier", "Télécharger le CSV"],
    "core:settings": ["Règles de paie", "Jours ouvrés et jours fériés", "Devise"],
    "core:audit_log": ["Journal d’audit", "Qui a modifié quoi, quand et pourquoi"],
    "attendance:approvals": ["Validations", "Appels envoyés par les chefs d’équipe"],
    "payroll:cash_sheets": ["Feuilles de caisse", "Jour de paie"],
    "payroll:cash_sheet": ["Feuille de caisse", "Rapprochement de caisse", "Fonds remis"],
    "payroll:periods": ["Périodes de paie", "Statut de la période"],
    "payroll:advances": ["Avances sur salaire", "Accorder une avance"],
    "workers:list": ["Ajouter un ouvrier", "Doit (avance)"],
    "workers:detail": ["Historique des tarifs", "Paiements récents", "septembre 2025"],
    "workers:create": ["Nouvel ouvrier", "Enregistrer l’ouvrier"],
    "workers:edit": ["Modifier l’ouvrier", "Paramètres hérités"],
    "worksites:list": ["Ouvriers affectés", "Rechercher des chantiers"],
    "worksites:create": ["Nouveau chantier", "Enregistrer le chantier"],
    "worksites:detail": ["Équipe du jour", "Paramètres de paie de ce chantier"],
    "worksites:crew": ["Retirer de l’équipe", "Sur le chantier depuis"],
    "worksites:attendance": ["Historique des présences"],
    "worksites:pay": ["Fonds remis au chantier", "Feuilles de caisse de ce chantier"],
    "worksites:settings": ["Enregistrer les paramètres du chantier", "Horaires et appel"],
    "worksites:assign": ["Affecter à ce chantier"],
    "worksites:edit": ["Modifier le chantier"],
    "accounts:users": ["Utilisateurs et rôles", "Ce que chaque rôle peut faire"],
    "accounts:user_create": ["Inviter un utilisateur", "Coordonnées et rôle"],
    "accounts:user_edit": ["Définir le code PIN", "Téléphones"],
}
SIDEBAR_FR = ["Aujourd’hui", "Validations", "Feuilles de caisse", "Périodes de paie", "Rapports"]


def test_every_office_page_renders_in_french(client, scenario):
    client.force_login(scenario["admin"])
    _pick_language(client)
    pages = _office_pages(scenario)
    assert set(pages) == set(EXPECTED_FR)
    phrases = _english_phrases()

    for name, url in pages.items():
        response = client.get(url, headers=FR)
        assert response.status_code == 200, name
        page = response.content.decode()
        assert '<html lang="fr">' in page, name
        text = _visible_text(page)
        for expected in SIDEBAR_FR + EXPECTED_FR[name]:
            assert expected in text, (name, expected)
        leftovers = [p for p in phrases if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", text)]
        assert not leftovers, (name, leftovers)


def test_sign_in_page_renders_in_french(client, db):
    text = _visible_text(client.get(reverse("login"), headers=FR).content.decode())

    assert "Se connecter" in text
    assert "Chef d’équipe ?" in text
    assert "Sign in" not in text


# --- status letters -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "letters"),
    [("en", ["P", "A", "½", "L"]), ("fr", ["P", "A", "½", "R"])],
)
def test_status_letters_follow_the_language(language, letters):
    with translation.override(language):
        assert [status_letter(s) for s in ("present", "absent", "half_day", "late")] == letters
        # The one-letter model codes are accepted too; unknown statuses get a dash.
        assert status_letter("L") == letters[3]
        assert status_letter("not_recorded") == "–"


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", ">L</strong>Late</span>"), ("fr", ">R</strong>Retard</span>")],
)
def test_status_badge_letter_and_word(language, expected):
    template = Template('{% include "components/status_badge.html" with status="late" %}')
    with translation.override(language):
        rendered = template.render(Context({}))
    assert re.sub(r"\s+", " ", rendered).replace("> ", ">").find(expected) != -1, rendered


@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "P 2 · ½ 0 · L 1 · A 1"), ("fr", "P 2 · ½ 0 · R 1 · A 1")],
)
def test_site_overview_counts_use_translated_letters(client, scenario, language, expected):
    client.force_login(scenario["admin"])
    url = reverse("worksites:detail", args=[scenario["site"].pk])
    _pick_language(client, language)

    page = client.get(url, headers={"accept-language": language}).content.decode()

    assert expected in page


def test_letters_in_french_on_approvals_reports_and_worker_calendar(client, scenario):
    client.force_login(scenario["admin"])
    _pick_language(client)

    approvals = client.get(reverse("attendance:approvals"), headers=FR).content.decode()
    assert '<span aria-hidden="true">R</span><span class="sr-only">Retard</span>' in approvals
    assert '<span aria-hidden="true">L</span>' not in approvals

    reports = client.get(
        reverse("dashboard:reports"),
        {"period": "custom", "start": MON.isoformat(), "end": TUE.isoformat()},
        headers=FR,
    ).content.decode()
    assert '<abbr title="Retard" class="no-underline">R</abbr>' in reports

    kofi_calendar = client.get(
        reverse("workers:detail", args=[scenario["yao"].pk]), {"month": "2025-09"}, headers=FR
    ).content.decode()
    assert "Retard" in kofi_calendar and ">R</strong>" in kofi_calendar


def test_payslip_legend_uses_french_letters(client, scenario):
    from tests.test_payroll_views import _pdf_text

    client.force_login(scenario["manager"])
    line = line_of(scenario["site"], MON, scenario["yao"])

    with translation.override("en"):
        english = _pdf_text(client.get(reverse("payroll:payslip", args=[line.pk])).content)
    _pick_language(client)
    french = _pdf_text(client.get(reverse("payroll:payslip", args=[line.pk]), headers=FR).content)

    assert "P present · ½ half day · L late · A absent".encode("cp1252").decode("latin-1") in (
        english
    )
    legend = "P présent · ½ demi-journée · R retard · A absent"
    assert legend.encode("cp1252").decode("latin-1") in french
    assert "Bulletin de paie" in french


# --- dates ----------------------------------------------------------------------------------


def test_dates_use_french_day_and_month_names():
    day = datetime.date(2026, 10, 30)
    # The office templates translate their date formats: French writes "N" ("oct.").
    template = Template(
        '{{ d|date:_("l j F Y") }} · {{ d|date:_("D j M") }} · {{ d|date:_("F Y") }}'
    )
    expected_fr = "vendredi 30 octobre 2026 · ven 30 oct. · octobre 2026"
    expected_en = "Friday 30 October 2026 · Fri 30 Oct · October 2026"

    with translation.override("fr"):
        assert template.render(Context({"d": day})) == expected_fr
        assert labels.short_date(day) == "ven 30 oct."
        assert labels.short_date(datetime.date(2026, 6, 1)) == "lun 1 juin"
    with translation.override("en"):
        assert template.render(Context({"d": day})) == expected_en
        assert labels.short_date(day) == "Fri 30 Oct"


def test_today_page_subtitle_is_a_french_date(client, scenario):
    client.force_login(scenario["admin"])
    _pick_language(client)
    now = datetime.datetime(2026, 10, 30, 9, 15, tzinfo=datetime.UTC)

    with mock.patch("django.utils.timezone.now", return_value=now):
        page = client.get(reverse("dashboard:home"), headers=FR).content.decode()

    assert "vendredi 30 octobre 2026" in page


# --- the API --------------------------------------------------------------------------------


def test_api_errors_come_back_in_french(client, company):
    wrong_header = client.get("/api/v1/me/", headers={"authorization": "Device a b", **FR})
    assert wrong_header.status_code == 401
    assert wrong_header.json()["message"] == (
        "En-tête Authorization invalide. Utilisez : Device <token>."
    )

    unknown = client.get("/api/v1/nothing-here/", headers=FR)
    assert unknown.json()["code"] == "not_found"
    assert unknown.json()["message"] == "Non trouvé."  # DRF's own French catalogue


def test_api_validation_errors_come_back_in_french(client, company):
    response = client.post("/api/v1/auth/pin/", {}, content_type="application/json", headers=FR)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["message"] == "Données invalides."
    assert "Ce champ est obligatoire." in str(body["details"])


def test_api_wrong_pin_message_in_french(client, company, scenario):
    foreman = scenario["foreman"]
    response = client.post(
        "/api/v1/auth/pin/",
        {"phone": foreman.phone, "pin": "0000", "device_id": "t-1"},
        content_type="application/json",
        headers=FR,
    )

    assert response.status_code in (400, 401)
    assert response.json()["message"].startswith(("Code PIN incorrect", "Numéro de téléphone"))


def test_api_stays_english_without_accept_language(client, company):
    response = client.post("/api/v1/auth/pin/", {}, content_type="application/json")

    assert response.json()["message"] == "Invalid input."
