"""Phone numbers in the E.164 shape providers expect (``+22890010101``).

Numbers are typed by hand in many ways ("90 01 01 01", "0022890010101", "+228 90-01-01-01").
``normalise_phone`` keeps the digits, turns a ``00`` international prefix into ``+``, and
adds the company's default country code to a national number (dropping one leading
trunk ``0``). Without a country code, a national number cannot be used.
"""

import re

_SEPARATORS = re.compile(r"[\s\-.()/]")
MIN_DIGITS = 8
MAX_DIGITS = 15  # E.164


def normalise_phone(raw: str | None, default_country_code: str = "") -> str | None:
    """``"+228 90 01 01 01"`` → ``"+22890010101"``; ``None`` if it cannot be used."""
    number = _SEPARATORS.sub("", raw or "")
    if not number:
        return None
    if number.startswith("+"):
        digits = number[1:]
    elif number.startswith("00"):
        digits = number[2:]
    else:
        code = (default_country_code or "").strip().lstrip("+")
        if not code.isdigit():
            return None
        national = number[1:] if number.startswith("0") else number
        digits = code + national
    if not digits.isdigit() or not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        return None
    return f"+{digits}"
