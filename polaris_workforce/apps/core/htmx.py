"""htmx request headers.

The office dashboard boosts its links and forms (``hx-boost`` on the app layout): a
sidebar click or a plain form post is sent by htmx, gets the full page back and swaps
``#main`` (static/js/app.js). Those requests carry ``HX-Request`` *and* ``HX-Boosted``
and must be answered exactly like a browser request (full page, redirects, messages).

Only an explicit ``hx-get`` / ``hx-post`` wants a fragment back: test it with
``wants_fragment``, never with ``HX-Request`` alone.
"""

from __future__ import annotations

from django.http import HttpRequest


def wants_fragment(request: HttpRequest) -> bool:
    """True for an explicit htmx request (hx-get, hx-post…), False for boosted navigation."""
    headers = request.headers
    return bool(headers.get("HX-Request")) and not headers.get("HX-Boosted")
