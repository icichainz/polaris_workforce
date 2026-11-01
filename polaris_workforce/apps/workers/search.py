"""Finding workers by name, worker number or phone, as people type them.

``Worker.search_text`` keeps the searchable values folded: lower case, without accents
(SQLite's LIKE ignores case for ASCII letters only, so "élodie" never found "Élodie"),
and the phone without its spaces and dashes. A search is one ``contains`` on that column
per word typed, every word must match. ``Worker.objects.search(text)`` runs it.
"""

import re
import unicodedata

# The fields ``search_text`` is made from.
SOURCE_FIELDS = ("first_name", "last_name", "number", "phone")

_PHONE_LIKE = re.compile(r"^\+?[\d\s\-.()]+$")
_PHONE_SEPARATORS = re.compile(r"[\s\-.()]")


def fold(value: str) -> str:
    """``"Élodie"`` → ``"elodie"``: lower case, accents removed."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _compact_phone(value: str) -> str:
    return _PHONE_SEPARATORS.sub("", value or "")


def search_text(worker) -> str:
    """The value stored in ``worker.search_text``."""
    parts = [fold(worker.first_name), fold(worker.last_name), fold(worker.number)]
    if worker.phone:
        parts.append(_compact_phone(worker.phone))
    return " ".join(part for part in parts if part)


def search_terms(text: str) -> list[str]:
    """What each typed word is looked for as: folded, and a number typed with spaces or
    dashes ("90 12-34") is one term without them, as phones are stored."""
    text = (text or "").strip()
    if not text:
        return []
    if _PHONE_LIKE.match(text) and any(ch.isdigit() for ch in text):
        return [_compact_phone(text)]
    return [
        _compact_phone(word) if _PHONE_LIKE.match(word) else fold(word) for word in text.split()
    ]
