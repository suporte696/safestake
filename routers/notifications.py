import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import get_db
from models import Notification
from routers.auth import ensure_admin_user, fetch_current_user
from services.jobs import run_scheduled_jobs

router = APIRouter()
logger = logging.getLogger(__name__)


def serialize_notification(item: Notification) -> dict:
    return {
        "id": item.id,
        "type": item.type,
        "title": item.title,
        "message": item.message,
        "action_url": item.action_url,
        "read": item.read_at is not None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("/api/notifications")
def list_notifications(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para visualizar notificações.")

    try:
        run_scheduled_jobs(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao executar jobs na listagem de notificações")
    stmt = (
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(20)
    )
    items = db.execute(stmt).scalars().all()
    return {"notifications": [serialize_notification(item) for item in items]}


@router.get("/api/notifications/unread-count")
def unread_count(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para visualizar notificações.")

    try:
        run_scheduled_jobs(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao executar jobs na contagem de notificações")
    count_stmt = select(func.count(Notification.id)).where(
        Notification.user_id == user.id,
        Notification.read_at.is_(None),
    )
    count = db.execute(count_stmt).scalar_one()
    return {"unread": int(count)}


@router.post("/api/notifications/{notification_id}/read")
def mark_notification_as_read(notification_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para atualizar notificações.")
    item = db.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)
    ).scalars().first()
    if not item:
        raise HTTPException(status_code=404, detail="Notificação não encontrada.")
    if item.read_at is None:
        from datetime import datetime, timezone

        item.read_at = datetime.now(timezone.utc)
        db.commit()
    return {"success": True}


@router.post("/api/jobs/run-deadlines")
def run_deadline_jobs(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login.")
    ensure_admin_user(user)
    try:
        result = run_scheduled_jobs(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao executar jobs manualmente")
        raise HTTPException(status_code=500, detail="Falha ao executar jobs agendados. Verifique os logs.") from None
    return {"success": True, "jobs": result}
