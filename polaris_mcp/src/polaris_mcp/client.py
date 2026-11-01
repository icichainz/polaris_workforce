"""HTTP client for the Polaris office API (``/api/v1/office/``).

Every call carries the agent token. The server does all the checking (role, sites,
company, what the token may do); this client only speaks HTTP and turns the API's error
format ``{"code", "message", "details"}`` into ``PolarisError``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

USER_AGENT = "polaris-mcp/0.1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class PolarisError(Exception):
    """An error answer from Polaris, or Polaris could not be reached."""

    def __init__(
        self, message: str, *, code: str = "error", status: int | None = None, details=None
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}

    def __str__(self) -> str:
        if not self.details:
            return self.message
        fields = "; ".join(
            f"{name}: {' '.join(map(str, errors)) if isinstance(errors, list) else errors}"
            for name, errors in self.details.items()
        )
        return f"{self.message} ({fields})"


@dataclass(frozen=True)
class Settings:
    url: str
    token: str

    @classmethod
    def from_env(cls) -> Settings:
        url = os.environ.get("POLARIS_URL", "").strip().rstrip("/")
        token = os.environ.get("POLARIS_TOKEN", "").strip()
        missing = [
            name for name, value in (("POLARIS_URL", url), ("POLARIS_TOKEN", token)) if not value
        ]
        if missing:
            raise SystemExit(
                f"polaris-mcp: set {' and '.join(missing)} (the Polaris address and an agent "
                "token from Polaris > AI assistant access)."
            )
        if not token.startswith("pol_"):
            raise SystemExit("polaris-mcp: POLARIS_TOKEN must be an agent token (pol_…).")
        if not url.startswith("https://") and not _is_local(url):
            raise SystemExit(
                "polaris-mcp: POLARIS_URL must use https:// (except on this computer)."
            )
        return cls(url=url, token=token)


def _is_local(url: str) -> bool:
    host = httpx.URL(url).host
    return url.startswith("http://") and host in {"localhost", "127.0.0.1", "::1"}


class PolarisClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=f"{settings.url}/api/v1/office/",
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=TIMEOUT,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise PolarisError(f"Polaris could not be reached: {exc}", code="unreachable") from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_success and isinstance(body, dict):
            return body
        if isinstance(body, dict) and "message" in body:
            raise PolarisError(
                str(body["message"]),
                code=str(body.get("code", "error")),
                status=response.status_code,
                details=body.get("details") or {},
            )
        raise PolarisError(
            f"Polaris answered HTTP {response.status_code}.", status=response.status_code
        )

    # --- reads -----------------------------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "me/")

    async def today(self) -> dict[str, Any]:
        return await self._request("GET", "today/")

    async def approvals(self) -> dict[str, Any]:
        return await self._request("GET", "approvals/")

    async def day(self, site_id: int, date: str) -> dict[str, Any]:
        return await self._request("GET", f"sites/{site_id}/days/{date}/")

    async def cash_sheet(self, site_id: int, date: str) -> dict[str, Any]:
        return await self._request("GET", f"sites/{site_id}/cash-sheets/{date}/")

    async def workers(self, query: str = "", site_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if query:
            params["q"] = query
        if site_id is not None:
            params["site"] = site_id
        return await self._request("GET", "workers/", params=params)

    async def worker(self, worker_id: int, date_from: str | None, date_to: str | None):
        params = {k: v for k, v in (("from", date_from), ("to", date_to)) if v}
        return await self._request("GET", f"workers/{worker_id}/", params=params)

    # --- decisions -------------------------------------------------------------------------

    async def preview_decision(self, day_id: int, decision: str, note: str = ""):
        body = {"decision": decision, "note": note}
        return await self._request("POST", f"days/{day_id}/decision-preview/", json=body)

    async def confirm_decision(self, confirmation: str) -> dict[str, Any]:
        return await self._request("POST", "decisions/", json={"confirmation": confirmation})
