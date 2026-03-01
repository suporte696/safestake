from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Notification

RESULT_REMINDER_TYPE = "RESULT_DEADLINE_REMINDER"


def _notification_exists(db: Session, user_id: int, n_type: str, action_url: str | None) -> bool:
    stmt = select(Notification.id).where(
        Notification.user_id == user_id,
        Notification.type == n_type,
        Notification.action_url == action_url,
    )
    return db.execute(stmt).scalars().first() is not None


def _create_notification(
    db: Session,
    *,
    user_id: int,
    n_type: str,
    title: str,
    message: str,
    action_url: str | None = None,
) -> None:
    if _notification_exists(db, user_id, n_type, action_url):
        return
    db.add(
        Notification(
            user_id=user_id,
            type=n_type,
            title=title,
            message=message,
            action_url=action_url,
        )
    )


def run_result_deadline_jobs(db: Session) -> dict:
    # Fluxo Single-Player (MVP 2.0): desativado bloqueio/lembrete automático por prazo de resultado.
    return {"reminded": 0, "blocked": 0}
