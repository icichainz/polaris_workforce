"""``polaris-mcp``: run the MCP server over stdio (what Claude Desktop launches).

``polaris-mcp check`` instead checks the address and the token, and says who they act for.
Configuration: ``POLARIS_URL`` and ``POLARIS_TOKEN``, else the file ``polaris login`` wrote.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .client import PolarisClient, PolarisError, Settings, SettingsError
from .server import build_server


async def _check(client: PolarisClient) -> int:
    try:
        me = await client.me()
    except PolarisError as exc:
        print(f"Polaris refused: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    rights = "read and approve roll calls" if me["token"]["can_approve"] else "read only"
    sites = ", ".join(site["name"] for site in me["sites"]) or "no sites"
    print(f"Connected to {me['company']['name']} as {me['user']['name']} ({me['user']['role']}).")
    print(f"Token '{me['token']['name']}': {rights}, until {me['token']['expires_at'][:10]}.")
    print(f"Sites: {sites}.")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    # httpx logs every request at INFO; Claude Desktop keeps stderr in its MCP logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.load()
    except SettingsError as exc:
        raise SystemExit(f"polaris-mcp: {exc}") from exc
    client = PolarisClient(settings)
    if args[:1] == ["check"]:
        raise SystemExit(asyncio.run(_check(client)))
    if args:
        raise SystemExit("usage: polaris-mcp [check]")
    build_server(client).run("stdio")


if __name__ == "__main__":
    main()
