"""Shared slowapi ``Limiter`` instance.

Extracted into its own module so route packages (e.g. ``gecko_api.routes``)
can apply ``@limiter.limit(...)`` decorators without importing
``gecko_api.main`` (which would re-import the route module and create a
circular import). ``main.py`` imports the same instance and binds it to
``app.state.limiter`` for slowapi's middleware + exception handler.
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request: Request) -> str:
    """Bucket on Authorization header when present; fall back to remote IP.

    Mirrors the behaviour previously inlined in ``main.py``. Authenticated
    callers can't dodge limits by spoofing IPs, and unauthenticated public
    routes (e.g. ``/v1/verdict/{hash}``) bucket on the connecting IP.
    """
    auth = request.headers.get("authorization")
    if auth:
        return auth
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)


__all__ = ["limiter"]
