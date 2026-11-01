"""Phone numbers as the phone sign-in compares them (``User.phone_normalized``)."""

import re


def normalize_phone(phone: str) -> str:
    """Digits only, keeping a leading ``+``: ``"+228 90-11 22 33"`` → ``"+22890112233"``."""
    phone = (phone or "").strip()
    digits = re.sub(r"\D", "", phone)
    return f"+{digits}" if phone.startswith("+") and digits else digits
