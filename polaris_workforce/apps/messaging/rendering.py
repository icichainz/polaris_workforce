"""Message texts: one Django template per message type and language.

``templates/messaging/texts/<kind>.<language>.txt`` (English and French), rendered with
that language active so dates use its month and weekday names. A language without its
own template falls back to English. The result is one line (whitespace collapsed).
"""

from django.template.loader import render_to_string
from django.utils import translation

from apps.core.choices import Language

TEMPLATE = "messaging/texts/{kind}.{language}.txt"
FALLBACK_LANGUAGE = Language.ENGLISH


def language_for(company_settings, user=None) -> str:
    """Users: their own language, else the company's. Workers: the company's."""
    language = getattr(user, "language", "") or company_settings.language
    return language if language in Language.values else FALLBACK_LANGUAGE


def render_text(kind: str, language: str, context: dict) -> str:
    names = [TEMPLATE.format(kind=kind, language=language)]
    if language != FALLBACK_LANGUAGE:
        names.append(TEMPLATE.format(kind=kind, language=FALLBACK_LANGUAGE))
    with translation.override(language):
        text = render_to_string(names, context)
    return " ".join(text.split())
