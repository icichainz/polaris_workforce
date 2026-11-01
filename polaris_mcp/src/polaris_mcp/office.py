"""What the MCP server and the CLI share on top of the HTTP client: the user's profile
(fetched once) and turning what people type ("bravo", "yesterday") into site ids and dates.

Polaris checks every id again; this only saves people from looking ids up.
"""

from __future__ import annotations

import datetime
from typing import Any

from .client import PolarisClient, PolarisError


class Office:
    def __init__(self, client: PolarisClient):
        self.client = client
        self._me: dict[str, Any] | None = None

    async def me(self, refresh: bool = False) -> dict[str, Any]:
        if self._me is None or refresh:
            self._me = await self.client.me()
        return self._me

    async def site_id(self, site: str) -> int:
        """A site id from its id, its name or part of its name, among the user's sites."""
        text = site.strip()
        if text.isdigit():
            return int(text)
        sites = (await self.me())["sites"]
        exact = [s for s in sites if s["name"].casefold() == text.casefold()]
        partial = [s for s in sites if text.casefold() in s["name"].casefold()]
        matches = exact or partial
        if len(matches) == 1:
            return matches[0]["id"]
        names = ", ".join(s["name"] for s in (matches or sites)) or "none"
        if not matches:
            raise PolarisError(f"No site of yours matches '{site}'. Your sites: {names}.")
        raise PolarisError(f"'{site}' matches several sites: {names}. Say which one.")

    async def date(self, text: str) -> str:
        """``YYYY-MM-DD``, ``today`` or ``yesterday`` (the company's today) as ISO date."""
        value = text.strip().lower()
        if value in {"today", "yesterday"}:
            today = datetime.date.fromisoformat((await self.me())["company"]["today"])
            day = today if value == "today" else today - datetime.timedelta(days=1)
            return day.isoformat()
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise PolarisError(f"'{text}' is not a date. Use YYYY-MM-DD.") from exc
