import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Investment, MatchResult, StakeOffer, Tournament, TournamentEscrow, Wallet
from routers.auth import ensure_admin_user, fetch_current_user

router = APIRouter()
logger = logging.getLogger(__name__)
MONEY_Q = Decimal("0.01")
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _ensure_wallet(db: Session, user_id: int, with_lock: bool = False) -> Wallet:
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if with_lock:
        stmt = stmt.with_for_update()
    wallet = db.execute(stmt).scalars().first()
    if wallet:
        return wallet
    wallet = Wallet(
        user_id=user_id,
        saldo_disponivel=Decimal("0"),
        saldo_bloqueado=Decimal("0"),
        saldo_em_jogo=Decimal("0"),
    )
    db.add(wallet)
    db.flush()
    return wallet


def q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def get_offer_escrow_locked(db: Session, offer: StakeOffer) -> TournamentEscrow:
    stmt = (
        select(TournamentEscrow)
        .where(TournamentEscrow.offer_id == offer.id)
        .with_for_update()
    )
    escrow = db.execute(stmt).scalars().first()
    if escrow:
        return escrow

    buyin = Decimal(str(offer.tournament.buyin or 0))
    markup = Decimal(str(offer.markup or 0))
    pct = Decimal(str(offer.total_disponivel_pct or 0))
    total_required = q_money((buyin * markup * pct) / Decimal("100"))
    escrow = TournamentEscrow(
        tournament_id=offer.tournament_id,
        offer_id=offer.id,
        total_required=total_required,
        total_collected=Decimal("0.00"),
        status="COLLECTING",
        deadline_at=offer.tournament.data_hora,
    )
    db.add(escrow)
    db.flush()
    return escrow


def sync_offer_escrow(db: Session, offer: StakeOffer) -> TournamentEscrow:
    escrow = get_offer_escrow_locked(db, offer)
    total_collected = db.execute(
        select(func.coalesce(func.sum(Investment.valor_investido), 0)).where(Investment.offer_id == offer.id)
    ).scalar_one()
    collected = q_money(Decimal(str(total_collected or 0)))
    escrow.total_collected = collected

    if escrow.total_required > 0 and collected >= Decimal(str(escrow.total_required)):
        escrow.status = "COMPLETE"
        escrow.completed_at = datetime.now(timezone.utc)
        offer.escrow_status = "COMPLETE"
    else:
        escrow.status = "COLLECTING"
        offer.escrow_status = "COLLECTING"
        escrow.completed_at = None

    return escrow


def force_complete_and_release_escrow(db: Session, offer: StakeOffer) -> dict:
    """Fecha o escrow com o total já coletado e libera para o jogador (iniciar sem meta total)."""
    escrow = get_offer_escrow_locked(db, offer)
    if escrow.status == "REFUNDED":
        return {"released_total": q_money(Decimal("0"))}
    collected = q_money(Decimal(str(escrow.total_collected or 0)))
    if collected <= 0:
        return {"released_total": q_money(Decimal("0"))}
    if escrow.status == "COLLECTING":
        escrow.total_required = collected
        escrow.status = "COMPLETE"
        escrow.completed_at = datetime.now(timezone.utc)
        offer.escrow_status = "COMPLETE"
    return release_offer_escrow_to_player(db, offer)


def release_offer_escrow_to_player(db: Session, offer: StakeOffer) -> dict:
    escrow = get_offer_escrow_locked(db, offer)
    if escrow.status != "COMPLETE":
        return {"released_total": q_money(Decimal("0"))}
    # Evita dupla liberação em chamadas repetidas: após iniciar/finalizar, o release já ocorreu.
    if offer.tournament and offer.tournament.status != "Aberto":
        return {"released_total": q_money(Decimal("0"))}

    player_wallet = _ensure_wallet(db, offer.player_id, with_lock=True)

    collected = q_money(Decimal(str(escrow.total_collected or 0)))
    player_em_jogo = q_money(Decimal(str(player_wallet.saldo_em_jogo or 0)))
    released_total = q_money(min(collected, player_em_jogo))
    if collected > 0 and released_total < collected:
        logger.warning(
            "release_offer_escrow: reconciling mismatch em_jogo (%s) < collected (%s) for offer_id=%s player_id=%s",
            player_em_jogo, collected, offer.id, offer.player_id,
        )
        released_total = collected
    if released_total > 0:
        player_wallet.saldo_em_jogo = q_money(max(Decimal("0"), player_em_jogo - released_total))
        player_wallet.saldo_disponivel = q_money(Decimal(str(player_wallet.saldo_disponivel or 0)) + released_total)
    if offer.tournament and offer.tournament.status == "Aberto":
        offer.tournament.status = "Jogando"
    return {"released_total": released_total}


def refund_offer_escrow(db: Session, offer: StakeOffer, reason: str = "") -> dict:
    escrow = get_offer_escrow_locked(db, offer)
    if escrow.status == "REFUNDED":
        return {"refunded_total": q_money(Decimal("0"))}

    investments = db.execute(
        select(Investment).where(Investment.offer_id == offer.id).with_for_update()
    ).scalars().all()
    refunded_total = Decimal("0")
    for inv in investments:
        amount = q_money(Decimal(str(inv.valor_investido or 0)))
        backer_wallet = _ensure_wallet(db, inv.backer_id, with_lock=True)
        backer_wallet.saldo_disponivel = q_money(Decimal(str(backer_wallet.saldo_disponivel or 0)) + amount)
        em_jogo = Decimal(str(backer_wallet.saldo_em_jogo or 0))
        backer_wallet.saldo_em_jogo = q_money(max(Decimal("0"), em_jogo - amount))
        inv.lucro_recebido = Decimal("0")
        refunded_total += amount

    refunded_total = q_money(refunded_total)

    if refunded_total > 0:
        player_wallet = _ensure_wallet(db, offer.player_id, with_lock=True)
        em_jogo_player = Decimal(str(player_wallet.saldo_em_jogo or 0))
        player_wallet.saldo_em_jogo = q_money(max(Decimal("0"), em_jogo_player - refunded_total))

    escrow.status = "REFUNDED"
    escrow.refunded_at = datetime.now(timezone.utc)
    offer.escrow_status = "REFUNDED"
    offer.vendido_pct = Decimal("0")
    if reason:
        offer.tournament.status = "Aberto"
    return {"refunded_total": refunded_total}


def auto_refund_expired_escrows(db: Session) -> int:
    now_sp = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    stmt = (
        select(TournamentEscrow)
        .where(TournamentEscrow.status == "COLLECTING")
        .where(TournamentEscrow.deadline_at.is_not(None))
        .where(TournamentEscrow.deadline_at < now_sp)
        .options(joinedload(TournamentEscrow.offer).joinedload(StakeOffer.tournament))
        .with_for_update(of=TournamentEscrow)
    )
    escrows = db.execute(stmt).scalars().all()
    refunded = 0
    for esc in escrows:
        offer = esc.offer
        has_result = db.execute(select(MatchResult.id).where(MatchResult.tournament_id == offer.tournament_id)).scalars().first()
        if has_result:
            continue
        sync_offer_escrow(db, offer)
        if esc.status == "COLLECTING":
            refund_offer_escrow(db, offer, reason="DEADLINE_EXPIRED")
            refunded += 1
    return refunded


@router.get("/api/escrow/{offer_id}/status")
def escrow_status(offer_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login.")

    auto_refund_expired_escrows(db)
    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id)
        .with_for_update(of=StakeOffer)
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    if user.tipo != "admin" and user.id not in {offer.player_id}:
        has_investment = db.execute(
            select(Investment.id).where(Investment.offer_id == offer.id, Investment.backer_id == user.id)
        ).scalars().first()
        if not has_investment:
            raise HTTPException(status_code=403, detail="Sem acesso a este escrow.")

    escrow = sync_offer_escrow(db, offer)
    db.commit()

    return {
        "offer_id": offer.id,
        "escrow_status": escrow.status,
        "offer_escrow_status": offer.escrow_status,
        "total_required": float(escrow.total_required),
        "total_collected": float(escrow.total_collected),
        "deadline_at": escrow.deadline_at.isoformat() if escrow.deadline_at else None,
        "completed_at": escrow.completed_at.isoformat() if escrow.completed_at else None,
        "refunded_at": escrow.refunded_at.isoformat() if escrow.refunded_at else None,
    }


@router.post("/api/escrow/{offer_id}/refund")
def escrow_refund(offer_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id)
        .with_for_update(of=StakeOffer)
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")

    if user.tipo != "admin" and user.id != offer.player_id:
        raise HTTPException(status_code=403, detail="Apenas admin ou dono da oferta pode estornar.")
    if user.tipo == "admin":
        ensure_admin_user(user)

    offer.tournament = db.get(Tournament, offer.tournament_id)
    sync_offer_escrow(db, offer)
    result = refund_offer_escrow(db, offer, reason="MANUAL_REFUND")
    db.commit()

    return {"success": True, "offer_id": offer_id, "refunded_total": float(result["refunded_total"])}
