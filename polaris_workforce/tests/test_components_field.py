"""``components/field.html`` and ``money_field.html``: one aria-describedby per control."""

import re

import pytest
from django import forms
from django.template.loader import render_to_string


class _Form(forms.Form):
    name = forms.CharField(help_text="As on the ID card.")
    note = forms.CharField(help_text="Shown to the foreman.", widget=forms.Textarea)
    trade = forms.ChoiceField(choices=[("mason", "Mason")], help_text="Main trade.")
    agree = forms.BooleanField(help_text="Tick to confirm.")
    plain = forms.CharField(required=False)
    rate = forms.IntegerField(help_text="Per day.")


def _control(html: str, field_id: str) -> str:
    """The opening tag of the control whose id is ``field_id``."""
    match = re.search(rf'<(input|textarea|select)\b[^>]*\bid="{field_id}"[^>]*>', html)
    assert match, f"no control with id {field_id}"
    return match.group(0)


def _render(bound, template="components/field.html", **extra):
    return render_to_string(template, {"field": bound, **extra})


def _ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


@pytest.mark.parametrize("name", ["name", "note", "trade", "agree"])
def test_help_text_gives_one_describedby_pointing_at_the_help(name):
    html = _render(_Form()[name])
    tag = _control(html, f"id_{name}")

    assert tag.count("aria-describedby") == 1
    described = re.search(r'aria-describedby="([^"]+)"', tag).group(1)
    assert described == f"id_{name}_helptext"
    # The id points at the element that holds the help text.
    assert re.search(rf'<p id="{described}"[^>]*>[^<]*\S', html)
    assert "aria-invalid" not in tag


@pytest.mark.parametrize("name", ["name", "note", "trade", "agree"])
def test_errors_give_one_describedby_pointing_at_the_error(name):
    form = _Form(data={})
    form.is_valid()
    html = _render(form[name])
    tag = _control(html, f"id_{name}")

    assert tag.count("aria-describedby") == 1
    assert tag.count("aria-invalid") == 1
    described = re.search(r'aria-describedby="([^"]+)"', tag).group(1)
    assert described == f"id_{name}_error"
    assert described in _ids(html)


def test_no_help_and_no_error_means_no_describedby():
    tag = _control(_render(_Form()["plain"]), "id_plain")
    assert "aria-describedby" not in tag


def test_help_override_without_field_help_text():
    html = _render(_Form()["plain"], help="Optional.")
    tag = _control(html, "id_plain")
    assert tag.count("aria-describedby") == 1
    assert 'aria-describedby="id_plain_helptext"' in tag
    assert "id_plain_helptext" in _ids(html)


def test_money_field_has_one_describedby():
    html = _render(_Form()["rate"], "components/money_field.html", currency="XOF")
    tag = _control(html, "id_rate")
    assert tag.count("aria-describedby") == 1
    described = re.search(r'aria-describedby="([^"]+)"', tag).group(1).split()
    assert described == ["id_rate_currency", "id_rate_helptext"]
    assert set(described) <= _ids(html)

    form = _Form(data={})
    form.is_valid()
    tag = _control(_render(form["rate"], "components/money_field.html", currency="XOF"), "id_rate")
    assert tag.count("aria-describedby") == 1
    assert tag.count("aria-invalid") == 1
