from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import MatchResult, Notification, StakeOffer, Tournament, User

RESULT_REMINDER_TYPE = "RESULT_DEADLINE_REMINDER"
RESULT_DEADLINE_BLOCK_TYPE = "RESULT_DEADLINE_BLOCK"
RESULT_DEADLINE_HOURS = 24
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


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
    target_role: str | None = None,
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
            target_role=target_role,
        )
    )


def run_result_deadline_jobs(db: Session) -> dict:
    """Notifica jogadores com torneios 'Jogando' sem resultado; após 24h da data do torneio, bloqueia a conta."""
    now_sp = datetime.now(LOCAL_TZ)
    now_naive = now_sp.replace(tzinfo=None) if now_sp.tzinfo else now_sp
    reminded = 0
    blocked = 0

    tournaments_jogando = db.execute(
        select(Tournament).where(Tournament.status == "Jogando")
    ).scalars().all()

    for t in tournaments_jogando:
        has_result = db.execute(
            select(MatchResult.id).where(MatchResult.tournament_id == t.id).limit(1)
        ).first() is not None
        if has_result:
            continue

        offer = db.execute(
            select(StakeOffer).where(StakeOffer.tournament_id == t.id).limit(1)
        ).scalars().first()
        if not offer:
            continue
        player_id = offer.player_id
        data_hora = t.data_hora
        if not data_hora:
            continue
        if getattr(data_hora, "tzinfo", None):
            data_hora_naive = data_hora.astimezone(LOCAL_TZ).replace(tzinfo=None)
        else:
            data_hora_naive = data_hora
        deadline_naive = data_hora_naive + timedelta(hours=RESULT_DEADLINE_HOURS)

        action_url = f"/player/results/new?tournament_id={t.id}"
        if now_naive >= deadline_naive:
            user = db.execute(select(User).where(User.id == player_id)).scalars().first()
            if user and not user.is_blocked:
                user.is_blocked = True
                user.blocked_reason = "Informe o resultado do torneio para desbloquear sua conta."
                user.blocked_at = datetime.now(timezone.utc)
                _create_notification(
                    db,
                    user_id=player_id,
                    n_type=RESULT_DEADLINE_BLOCK_TYPE,
                    title="Conta suspensa",
                    message="Informe o resultado do torneio para desbloquear sua conta.",
                    action_url=action_url,
                    target_role="jogador",
                )
                blocked += 1
        else:
            _create_notification(
                db,
                user_id=player_id,
                n_type=RESULT_REMINDER_TYPE,
                title="Informe o resultado",
                message=f"Você tem até 24h após o torneio para informar o resultado. Torneio: {t.nome or '#' + str(t.id)}",
                action_url=action_url,
                target_role="jogador",
            )
            reminded += 1

    if reminded or blocked:
        db.commit()
    return {"reminded": reminded, "blocked": blocked}
