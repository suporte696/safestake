import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from routers import (
    admin_router,
    auth_router,
    escrow_router,
    marketplace_router,
    notifications_router,
    payments_router,
    player_router,
)

app = FastAPI(title="SAFE STAKE")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "safe-stake-dev-secret"),
    session_cookie="safe_stake_session",
    same_site="lax",
)

app.include_router(auth_router)
app.include_router(marketplace_router)
app.include_router(payments_router)
app.include_router(admin_router)
app.include_router(player_router)
app.include_router(escrow_router)
app.include_router(notifications_router)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(HTTPException)
async def friendly_http_exception_handler(request: Request, exc: HTTPException):
    accepts = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accepts
    is_api = request.url.path.startswith("/api") or request.url.path.startswith("/webhooks")
    if wants_html and not is_api and exc.status_code in {401, 403}:
        is_logged = bool(request.session.get("user_id"))
        if is_logged:
            return RedirectResponse(url="/dashboard?access_error=1", status_code=303)
        return RedirectResponse(url="/login", status_code=303)
    return await http_exception_handler(request, exc)

