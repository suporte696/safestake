from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import CallSchedule, Investment, MatchResult, PrizeDistribution, StakeOffer, UserDocument, Wallet
from routers.auth import ensure_admin_user, fetch_current_user, get_wallet_summary

templates = Jinja2Templates(directory="templates")
router = APIRouter()
FEE_RATE = Decimal("0.08")
MONEY_Q = Decimal("0.01")


def q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def get_wallet_for_update(db: Session, user_id: int) -> Wallet:
    wallet = db.execute(select(Wallet).where(Wallet.user_id == user_id).with_for_update()).scalars().first()
    if not wallet:
        raise HTTPException(status_code=400, detail=f"Carteira não encontrada para usuário {user_id}.")
    return wallet


@router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = (
        select(UserDocument)
        .options(joinedload(UserDocument.user), joinedload(UserDocument.reviewed_by_user))
        .order_by(UserDocument.created_at.asc())
    )
    documents = db.execute(stmt).scalars().all()
    pending_docs = [doc for doc in documents if doc.status == "PENDING"]
    reviewed_docs = [doc for doc in documents if doc.status != "PENDING"][:30]
    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "pending_docs": pending_docs,
            "reviewed_docs": reviewed_docs,
            "requires_auth": True,
        },
    )


@router.post("/admin/kyc/{document_id}/approve")
def approve_kyc(document_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = select(UserDocument).where(UserDocument.id == document_id)
    document = db.execute(stmt).scalars().first()
    if document:
        document.status = "APPROVED"
        document.rejection_reason = None
        document.reviewed_by = user.id
        document.reviewed_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.post("/admin/kyc/{document_id}/reject")
def reject_kyc(
    document_id: int,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = select(UserDocument).where(UserDocument.id == document_id)
    document = db.execute(stmt).scalars().first()
    if document:
        document.status = "REJECTED"
        document.rejection_reason = reason.strip()[:255] if reason else None
        document.reviewed_by = user.id
        document.reviewed_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/admin/results", response_class=HTMLResponse)
def admin_results(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    pending_stmt = (
        select(MatchResult)
        .where(MatchResult.review_status == "PENDING")
        .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
        .order_by(MatchResult.submitted_at.asc(), MatchResult.id.asc())
    )
    pending_results = db.execute(pending_stmt).scalars().all()

    reviewed_stmt = (
        select(MatchResult)
        .where(MatchResult.review_status.in_(("APPROVED", "REJECTED")))
        .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
        .order_by(MatchResult.reviewed_at.desc(), MatchResult.id.desc())
        .limit(40)
    )
    reviewed_results = db.execute(reviewed_stmt).scalars().all()

    distribution_stmt = (
        select(PrizeDistribution)
        .where(PrizeDistribution.match_result_id.in_([item.id for item in reviewed_results] or [-1]))
    )
    distributions = db.execute(distribution_stmt).scalars().all()
    distribution_by_result: dict[int, list[PrizeDistribution]] = {}
    for item in distributions:
        distribution_by_result.setdefault(item.match_result_id, []).append(item)

    return templates.TemplateResponse(
        "admin_result_review.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "pending_results": pending_results,
            "reviewed_results": reviewed_results,
            "distribution_by_result": distribution_by_result,
            "requires_auth": True,
        },
    )


@router.post("/admin/results/{result_id}/approve")
def approve_result(result_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    with db.begin():
        result_stmt = (
            select(MatchResult)
            .where(MatchResult.id == result_id)
            .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
            .with_for_update()
        )
        result = db.execute(result_stmt).scalars().first()
        if not result:
            raise HTTPException(status_code=404, detail="Resultado não encontrado.")
        if result.review_status != "PENDING":
            raise HTTPException(status_code=400, detail="Resultado já foi revisado.")

        total_sent = q_money(Decimal(str(result.valor_enviado)))
        if total_sent < 0:
            raise HTTPException(status_code=400, detail="Valor enviado não pode ser negativo.")

        investment_stmt = (
            select(Investment)
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(StakeOffer.tournament_id == result.tournament_id)
            .options(joinedload(Investment.backer), joinedload(Investment.offer))
            .with_for_update()
        )
        investments = db.execute(investment_stmt).scalars().all()

        backer_gross_total = Decimal("0")
        fee_total = Decimal("0")
        for inv in investments:
            pct = Decimal(str(inv.pct_comprada or 0))
            invested_amount = q_money(Decimal(str(inv.valor_investido or 0)))
            gross_amount = q_money(total_sent * pct / Decimal("100"))
            gain_amount = gross_amount - invested_amount
            if gain_amount < 0:
                gain_amount = Decimal("0")
            platform_fee = q_money(gain_amount * FEE_RATE)
            net_amount = gross_amount - platform_fee

            backer_wallet = get_wallet_for_update(db, inv.backer_id)
            backer_wallet.saldo_disponivel = q_money(Decimal(str(backer_wallet.saldo_disponivel)) + net_amount)
            em_jogo = Decimal(str(backer_wallet.saldo_em_jogo or 0))
            backer_wallet.saldo_em_jogo = q_money(max(Decimal("0"), em_jogo - invested_amount))
            inv.lucro_recebido = q_money(net_amount - invested_amount)

            db.add(
                PrizeDistribution(
                    match_result_id=result.id,
                    recipient_type="BACKER",
                    recipient_user_id=inv.backer_id,
                    pct_base=pct,
                    invested_amount=invested_amount,
                    gross_amount=gross_amount,
                    platform_fee=platform_fee,
                    net_amount=net_amount,
                    processed_by_admin_id=user.id,
                )
            )
            backer_gross_total += gross_amount
            fee_total += platform_fee

        backer_gross_total = q_money(backer_gross_total)
        fee_total = q_money(fee_total)
        if backer_gross_total > total_sent:
            raise HTTPException(
                status_code=400,
                detail="Soma de percentuais dos apoiadores excede o valor enviado.",
            )

        player_amount = q_money(total_sent - backer_gross_total)
        player_wallet = get_wallet_for_update(db, result.player_id)
        player_wallet.saldo_disponivel = q_money(Decimal(str(player_wallet.saldo_disponivel)) + player_amount)

        db.add(
            PrizeDistribution(
                match_result_id=result.id,
                recipient_type="PLAYER",
                recipient_user_id=result.player_id,
                pct_base=Decimal("0"),
                invested_amount=Decimal("0"),
                gross_amount=player_amount,
                platform_fee=Decimal("0"),
                net_amount=player_amount,
                processed_by_admin_id=user.id,
            )
        )
        db.add(
            PrizeDistribution(
                match_result_id=result.id,
                recipient_type="PLATFORM",
                recipient_user_id=None,
                pct_base=Decimal("0"),
                invested_amount=Decimal("0"),
                gross_amount=fee_total,
                platform_fee=fee_total,
                net_amount=fee_total,
                processed_by_admin_id=user.id,
            )
        )

        result.review_status = "APPROVED"
        result.admin_verified = True
        result.rejection_reason = None
        result.reviewed_by = user.id
        result.reviewed_at = datetime.now(timezone.utc)

    return RedirectResponse(url="/admin/results", status_code=303)


@router.post("/admin/results/{result_id}/reject")
def reject_result(
    result_id: int,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = select(MatchResult).where(MatchResult.id == result_id)
    result = db.execute(stmt).scalars().first()
    if result:
        if result.review_status != "PENDING":
            raise HTTPException(status_code=400, detail="Resultado já foi revisado.")
        result.review_status = "REJECTED"
        result.admin_verified = False
        result.rejection_reason = reason.strip()[:255] if reason else None
        result.reviewed_by = user.id
        result.reviewed_at = datetime.now(timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin/results", status_code=303)


@router.get("/admin/calls", response_class=HTMLResponse)
def admin_calls(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = (
        select(CallSchedule)
        .options(joinedload(CallSchedule.user), joinedload(CallSchedule.reviewed_by_user))
        .order_by(CallSchedule.created_at.desc(), CallSchedule.id.desc())
    )
    schedules = db.execute(stmt).scalars().all()
    return templates.TemplateResponse(
        "admin_calls.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "schedules": schedules,
            "requires_auth": True,
        },
    )


@router.post("/admin/calls/{schedule_id}/status")
def update_call_status(
    schedule_id: int,
    request: Request,
    status: str = Form(...),
    call_link: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    normalized_status = status.strip().upper()
    if normalized_status not in {"CONFIRMED", "CANCELLED", "PENDING"}:
        raise HTTPException(status_code=400, detail="Status de call inválido.")
    schedule = db.execute(select(CallSchedule).where(CallSchedule.id == schedule_id)).scalars().first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
    schedule.status = normalized_status
    schedule.call_link = call_link.strip()[:255] if call_link else None
    schedule.reviewed_by = user.id
    schedule.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url="/admin/calls", status_code=303)
