"""The {% money %} / {% number %} template tags follow the company settings."""

from decimal import Decimal

import pytest
from django.template import Context, Template
from django.test import RequestFactory

from apps.core.display import format_amount, format_quantity
from apps.core.models import CompanySettings

pytestmark = pytest.mark.django_db


def render(source, **context):
    return Template("{% load polaris %}" + source).render(Context(context))


def set_company(**values):
    company = CompanySettings.load()
    for name, value in values.items():
        setattr(company, name, value)
    company.save()
    return company


@pytest.mark.parametrize(
    ("number_format", "amount", "expected"),
    [
        ("space", 1250000, "1 250 000 XOF"),
        ("comma", 1250000, "1,250,000 XOF"),
        ("dot", 1250000, "XOF 1.250.000"),
        ("space", 0, "0 XOF"),
        ("space", -3500, "-3 500 XOF"),
    ],
)
def test_money_with_zero_decimals(number_format, amount, expected):
    set_company(currency="XOF", currency_decimals=0, number_format=number_format)

    assert render("{% money amount %}", amount=amount) == expected


@pytest.mark.parametrize(
    ("number_format", "amount", "expected"),
    [
        ("space", 123450, "1 234.50 EUR"),
        ("comma", 123450, "1,234.50 EUR"),
        ("dot", 123450, "EUR 1.234,50"),
        ("space", 5, "0.05 EUR"),
        ("space", 0, "0.00 EUR"),
    ],
)
def test_money_with_two_decimals(number_format, amount, expected):
    set_company(currency="EUR", currency_decimals=2, number_format=number_format)

    assert render("{% money amount %}", amount=amount) == expected


def test_changing_settings_changes_every_amount():
    set_company(currency="XOF", currency_decimals=0)
    assert render("{% money 350000 %}") == "350 000 XOF"

    set_company(currency="USD", currency_decimals=2, number_format="comma")
    assert render("{% money 350000 %}") == "3,500.00 USD"


def test_period_snapshot_variant_keeps_its_own_currency():
    set_company(currency="EUR", currency_decimals=2)

    class Period:
        currency = "XOF"
        currency_decimals = 0

    assert render("{% money 3500 period %}", period=Period()) == "3 500 XOF"
    assert render('{% money 3500 currency="GHS" decimals=2 %}') == "35.00 GHS"


def test_plain_none_and_as_variable():
    set_company(currency="XOF", currency_decimals=0)

    assert render("{% money 412500 plain=True %}") == "412 500"
    assert render("{% money None %}") == "—"
    assert render("{% money 2000 as total %}[{{ total }}]") == "[2 000 XOF]"
    assert render("{% company_currency %}") == "XOF"


def test_floats_are_refused():
    with pytest.raises(TypeError):
        render("{% money amount %}", amount=12.5)


def test_number_tag():
    set_company(number_format="dot")

    assert render("{% number value %}", value=Decimal("2412.50")) == "2.412,5"
    assert render("{% number value %}", value=Decimal("88.00")) == "88"
    assert render("{% number value max_decimals=1 %}", value=Decimal("1.26")) == "1,3"


def test_company_loaded_once_per_request(django_assert_num_queries):
    set_company()
    request = RequestFactory().get("/")
    source = "{% money 1 %} {% money 2 %} {% number 3 %} {% company_currency %}"

    with django_assert_num_queries(1):
        render(source, request=request)
    with django_assert_num_queries(0):
        render(source, request=request)


def test_company_from_context_needs_no_query(django_assert_num_queries):
    company = set_company()

    with django_assert_num_queries(0):
        assert render("{% money 1500 %}", company_settings=company) == "1 500 XOF"


def test_python_helpers_match():
    company = set_company(currency="EUR", currency_decimals=2, number_format="comma")

    assert format_amount(123456789, company) == "1,234,567.89 EUR"
    assert format_amount(None, company) == "—"
    assert format_quantity(Decimal("-1234.5"), company) == "-1,234.5"
    with pytest.raises(TypeError):
        format_quantity(1.5, company)
