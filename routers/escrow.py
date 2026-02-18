from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Investment, MatchResult, StakeOffer, TournamentEscrow, Wallet
from routers.auth import ensure_admin_user, fetch_current_user

router = APIRouter()
MONEY_Q = Decimal("0.01")


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

    if escrow.status == "COLLECTING" and escrow.total_required > 0 and collected >= Decimal(str(escrow.total_required)):
        escrow.status = "COMPLETE"
        escrow.completed_at = datetime.now(timezone.utc)
        offer.escrow_status = "COMPLETE"
        player_wallet = db.execute(select(Wallet).where(Wallet.user_id == offer.player_id).with_for_update()).scalars().first()
        if not player_wallet:
            raise HTTPException(status_code=400, detail="Carteira do jogador não encontrada.")
        player_wallet.saldo_disponivel = q_money(Decimal(str(player_wallet.saldo_disponivel or 0)) + collected)
    elif escrow.status == "COLLECTING":
        offer.escrow_status = "COLLECTING"

    return escrow


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
        backer_wallet = db.execute(select(Wallet).where(Wallet.user_id == inv.backer_id).with_for_update()).scalars().first()
        if not backer_wallet:
            raise HTTPException(status_code=400, detail=f"Carteira não encontrada para apoiador {inv.backer_id}.")
        backer_wallet.saldo_disponivel = q_money(Decimal(str(backer_wallet.saldo_disponivel or 0)) + amount)
        em_jogo = Decimal(str(backer_wallet.saldo_em_jogo or 0))
        backer_wallet.saldo_em_jogo = q_money(max(Decimal("0"), em_jogo - amount))
        inv.lucro_recebido = Decimal("0")
        refunded_total += amount

    refunded_total = q_money(refunded_total)

    if escrow.status == "COMPLETE" and refunded_total > 0:
        player_wallet = db.execute(select(Wallet).where(Wallet.user_id == offer.player_id).with_for_update()).scalars().first()
        if not player_wallet:
            raise HTTPException(status_code=400, detail="Carteira do jogador não encontrada.")
        saldo = Decimal(str(player_wallet.saldo_disponivel or 0))
        if saldo < refunded_total:
            raise HTTPException(
                status_code=400,
                detail="Jogador sem saldo suficiente para estorno de escrow completo.",
            )
        player_wallet.saldo_disponivel = q_money(saldo - refunded_total)

    escrow.status = "REFUNDED"
    escrow.refunded_at = datetime.now(timezone.utc)
    offer.escrow_status = "REFUNDED"
    offer.vendido_pct = Decimal("0")
    if reason:
        offer.tournament.status = "Aberto"
    return {"refunded_total": refunded_total}


def auto_refund_expired_escrows(db: Session) -> int:
    now = datetime.now(timezone.utc)
    stmt = (
        select(TournamentEscrow)
        .where(TournamentEscrow.status == "COLLECTING")
        .where(TournamentEscrow.deadline_at.is_not(None))
        .where(TournamentEscrow.deadline_at < now)
        .options(joinedload(TournamentEscrow.offer).joinedload(StakeOffer.tournament))
        .with_for_update()
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

    with db.begin():
        auto_refund_expired_escrows(db)
        offer = db.execute(
            select(StakeOffer)
            .where(StakeOffer.id == offer_id)
            .options(joinedload(StakeOffer.tournament))
            .with_for_update()
        ).scalars().first()
        if not offer:
            raise HTTPException(status_code=404, detail="Oferta não encontrada.")
        if user.tipo != "admin" and user.id not in {offer.player_id}:
            has_investment = db.execute(
                select(Investment.id).where(Investment.offer_id == offer.id, Investment.backer_id == user.id)
            ).scalars().first()
            if not has_investment:
                raise HTTPException(status_code=403, detail="Sem acesso a este escrow.")

        escrow = sync_offer_escrow(db, offer)

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

    with db.begin():
        offer = db.execute(
            select(StakeOffer)
            .where(StakeOffer.id == offer_id)
            .options(joinedload(StakeOffer.tournament))
            .with_for_update()
        ).scalars().first()
        if not offer:
            raise HTTPException(status_code=404, detail="Oferta não encontrada.")

        if user.tipo != "admin" and user.id != offer.player_id:
            raise HTTPException(status_code=403, detail="Apenas admin ou dono da oferta pode estornar.")
        if user.tipo == "admin":
            ensure_admin_user(user)

        sync_offer_escrow(db, offer)
        result = refund_offer_escrow(db, offer, reason="MANUAL_REFUND")

    return {"success": True, "offer_id": offer_id, "refunded_total": float(result["refunded_total"])}
