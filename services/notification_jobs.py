from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from models import MatchResult, Notification, StakeOffer, Tournament, User

RESULT_REMINDER_TYPE = "RESULT_DEADLINE_REMINDER"
RESULT_BLOCK_TYPE = "RESULT_DEADLINE_BLOCK"


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
    now = datetime.now(timezone.utc)
    reminded = 0
    blocked = 0

    offers_stmt = (
        select(StakeOffer)
        .join(Tournament, StakeOffer.tournament_id == Tournament.id)
        .where(Tournament.data_hora.is_not(None))
        .where(Tournament.data_hora <= now)
        .where(StakeOffer.player_id.is_not(None))
    )
    offers = db.execute(offers_stmt).scalars().all()

    for offer in offers:
        tournament_id = offer.tournament_id
        player_id = offer.player_id
        result_exists = db.execute(
            select(MatchResult.id).where(
                and_(MatchResult.tournament_id == tournament_id, MatchResult.player_id == player_id)
            )
        ).scalars().first()
        if result_exists:
            continue

        tournament = db.execute(select(Tournament).where(Tournament.id == tournament_id)).scalars().first()
        if not tournament or not tournament.data_hora:
            continue
        elapsed = now - tournament.data_hora
        action_url = "/player/results/new"

        if elapsed >= timedelta(hours=10):
            before = _notification_exists(db, player_id, RESULT_REMINDER_TYPE, action_url)
            _create_notification(
                db,
                user_id=player_id,
                n_type=RESULT_REMINDER_TYPE,
                title="Resultado pendente",
                message=(
                    f"Envie o resultado do torneio '{tournament.nome}' em até 12 horas após o início da partida."
                ),
                action_url=action_url,
            )
            if not before:
                reminded += 1

        if elapsed >= timedelta(hours=12):
            player = db.execute(select(User).where(User.id == player_id)).scalars().first()
            if player and not player.is_blocked:
                player.is_blocked = True
                player.blocked_reason = "Não informou resultado da partida dentro de 12 horas."
                player.blocked_at = now
                blocked += 1
            _create_notification(
                db,
                user_id=player_id,
                n_type=RESULT_BLOCK_TYPE,
                title="Conta bloqueada por prazo",
                message=(
                    f"Sua conta foi bloqueada por não informar resultado do torneio '{tournament.nome}' em até 12 horas."
                ),
                action_url=action_url,
            )

    return {"reminded": reminded, "blocked": blocked}
