import os

from fastapi import FastAPI
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

