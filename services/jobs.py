from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import StakeOffer, Tournament, TournamentEscrow, Wallet
from routers.escrow import release_offer_escrow_to_player
from services.notification_jobs import run_result_deadline_jobs

logger = logging.getLogger(__name__)


def _q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def run_escrow_start_jobs(db: Session) -> dict:
    now_utc = datetime.now(timezone.utc)
    # IMPORTANTE: não usar joinedload + FOR UPDATE no Postgres.
    # Isso pode gerar LEFT OUTER JOIN e quebrar com "cannot be applied to nullable side".
    stmt = (
        select(TournamentEscrow)
        .where(TournamentEscrow.status == "COMPLETE")
        .with_for_update()
    )
    escrows = db.execute(stmt).scalars().all()
    released = 0

    for escrow in escrows:
        offer = db.execute(
            select(StakeOffer).where(StakeOffer.id == escrow.offer_id).with_for_update()
        ).scalars().first()
        if not offer:
            continue
        tournament = db.execute(
            select(Tournament).where(Tournament.id == offer.tournament_id)
        ).scalars().first()
        if not offer or not tournament:
            continue
        start_at_utc = _normalize_to_utc(tournament.data_hora)
        if not start_at_utc or start_at_utc > now_utc:
            continue

        # Evita reliberação: só processa se ainda houver saldo em jogo do jogador.
        player_wallet = db.execute(select(Wallet).where(Wallet.user_id == offer.player_id).with_for_update()).scalars().first()
        if not player_wallet:
            continue
        if _q_money(Decimal(str(player_wallet.saldo_em_jogo or 0))) <= 0:
            continue

        result = release_offer_escrow_to_player(db, offer)
        if _q_money(Decimal(str(result["released_total"]))) > 0:
            released += 1
    return {"released": released}


def run_scheduled_jobs(db: Session) -> dict:
    try:
        result_deadline = run_result_deadline_jobs(db)
    except Exception:
        logger.exception("Falha ao executar job de prazo de resultado")
        result_deadline = {"error": True}
    try:
        escrow_start = run_escrow_start_jobs(db)
    except Exception:
        logger.exception("Falha ao executar job de liberação de escrow")
        escrow_start = {"error": True}
    return {"result_deadline": result_deadline, "escrow_start": escrow_start}
