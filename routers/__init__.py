from .auth import router as auth_router
from .admin import router as admin_router
from .escrow import router as escrow_router
from .marketplace import router as marketplace_router
from .notifications import router as notifications_router
from .payments import router as payments_router
from .player import router as player_router

__all__ = [
    "marketplace_router",
    "auth_router",
    "payments_router",
    "admin_router",
    "player_router",
    "escrow_router",
    "notifications_router",
]
