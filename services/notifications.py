from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Notification, User


def create_notification(
    db: Session,
    *,
    user_id: int,
    n_type: str,
    title: str,
    message: str,
    action_url: str | None = None,
    target_role: str | None = None,
) -> Notification:
    item = Notification(
        user_id=user_id,
        type=n_type,
        title=title,
        message=message,
        action_url=action_url,
        target_role=target_role,
    )
    db.add(item)
    return item


def notify_all_admins(
    db: Session,
    *,
    n_type: str,
    title: str,
    message: str,
    action_url: str | None = None,
    target_role: str | None = "admin",
) -> int:
    admins = db.execute(select(User).where(User.tipo == "admin")).scalars().all()
    for admin in admins:
        create_notification(
            db,
            user_id=admin.id,
            n_type=n_type,
            title=title,
            message=message,
            action_url=action_url,
            target_role=target_role,
        )
    return len(admins)
