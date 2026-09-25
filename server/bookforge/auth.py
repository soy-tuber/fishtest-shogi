"""Request principal (design doc §9).

Handlers only read ``request.state.principal``; how it got there is this
module's business.

* ``BOOKFORGE_AUTH=stub`` (development, Phase 1): a fixed principal. Workers
  are ``{"kind": "worker", "token_id": <X-Bookforge-Dev-Token or "dev">}``,
  humans are an admin ``dev@localhost``.
* ``BOOKFORGE_AUTH=access`` (Phase 3): Cloudflare Access JWT verification.
  Not implemented yet, so the app refuses to start in that mode rather than
  run without authentication.
"""

import os

from starlette.responses import JSONResponse

AUTH_MODES = ("stub", "access")


def auth_mode():
    mode = os.environ.get("BOOKFORGE_AUTH", "access")
    if mode not in AUTH_MODES:
        raise RuntimeError(f"BOOKFORGE_AUTH must be one of {AUTH_MODES}")
    if mode == "access":
        raise RuntimeError(
            "BOOKFORGE_AUTH=access (Cloudflare Access JWT) arrives in Phase 3; "
            "set BOOKFORGE_AUTH=stub for local development"
        )
    return mode


def stub_principal(path, headers):
    if path.startswith("/api/"):
        token = headers.get("x-bookforge-dev-token", "dev")
        return {"kind": "worker", "token_id": token}
    return {"kind": "user", "email": "dev@localhost", "role": "admin"}


class PrincipalMiddleware:
    """ASGI middleware that attaches the principal and enforces the split
    between worker endpoints (/api/*) and human pages."""

    def __init__(self, app, mode):
        self.app = app
        self.mode = mode

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope["headers"]
        }
        principal = stub_principal(path, headers)
        if path.startswith("/api/") and principal["kind"] != "worker":
            await JSONResponse({"error": "forbidden"}, status_code=403)(
                scope, receive, send
            )
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
