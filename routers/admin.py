from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import CallSchedule, Investment, MatchResult, PrizeDistribution, StakeOffer, Tournament, TournamentEscrow, UserDocument, Wallet
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

    active_tournaments_stmt = (
        select(Tournament)
        .where(Tournament.status.in_(("Aberto", "Jogando")))
        .order_by(Tournament.data_hora.asc().nulls_last(), Tournament.id.desc())
    )
    active_tournaments = db.execute(active_tournaments_stmt).scalars().all()

    finalized_tournaments_stmt = (
        select(Tournament)
        .where(Tournament.status == "Finalizado")
        .order_by(Tournament.data_hora.desc().nulls_last(), Tournament.id.desc())
    )
    finalized_tournaments = db.execute(finalized_tournaments_stmt).scalars().all()
    finalized_ids = [item.id for item in finalized_tournaments]

    investments_by_tournament: dict[int, list[Investment]] = {}
    if finalized_ids:
        investment_stmt = (
            select(Investment)
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(StakeOffer.tournament_id.in_(finalized_ids))
            .options(joinedload(Investment.backer), joinedload(Investment.offer))
            .order_by(StakeOffer.tournament_id.asc(), Investment.id.asc())
        )
        for investment in db.execute(investment_stmt).scalars().all():
            if not investment.offer:
                continue
            investments_by_tournament.setdefault(investment.offer.tournament_id, []).append(investment)

    settlement_rows_by_tournament: dict[int, list[dict]] = {}
    for tournament in finalized_tournaments:
        rows: list[dict] = []
        for inv in investments_by_tournament.get(tournament.id, []):
            rows.append(
                {
                    "investment_id": inv.id,
                    "apoiador_nome": inv.backer.nome if inv.backer else f"Usuário #{inv.backer_id}",
                    "valor_investido": Decimal(str(inv.valor_investido or 0)),
                    "valor_receber": Decimal(str(inv.lucro_recebido or 0)),
                    "pix_key": (inv.backer.pix_key if inv.backer and inv.backer.pix_key else "-"),
                    "payout_status": inv.payout_status,
                }
            )
        settlement_rows_by_tournament[tournament.id] = rows

    return templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "pending_docs": pending_docs,
            "reviewed_docs": reviewed_docs,
            "active_tournaments": active_tournaments,
            "finalized_tournaments": finalized_tournaments,
            "settlement_rows_by_tournament": settlement_rows_by_tournament,
            "requires_auth": True,
        },
    )


@router.post("/admin/tournaments/create")
def create_admin_tournament_offer(
    request: Request,
    tournament_name: str = Form(...),
    buyin: float = Form(...),
    markup: float = Form(...),
    total_pct: float = Form(...),
    start_time: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    try:
        buyin_value = q_money(Decimal(str(buyin)))
        markup_value = Decimal(str(markup))
        total_pct_value = Decimal(str(total_pct))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Dados inválidos para criar torneio.")

    if buyin_value <= 0:
        raise HTTPException(status_code=400, detail="Buy-in deve ser maior que zero.")
    if markup_value <= 0:
        raise HTTPException(status_code=400, detail="Markup deve ser maior que zero.")
    if total_pct_value <= 0 or total_pct_value > 100:
        raise HTTPException(status_code=400, detail="Porcentagem à venda deve ser entre 0 e 100.")

    data_hora = None
    if start_time:
        try:
            data_hora = datetime.fromisoformat(start_time)
        except ValueError:
            raise HTTPException(status_code=400, detail="Data/hora inválida.")

    with db.begin():
        tournament = Tournament(
            nome=tournament_name.strip(),
            sharkscope_id="GGPoker",
            plataforma="GGPoker",
            buyin=buyin_value,
            data_hora=data_hora,
            status="Aberto",
        )
        db.add(tournament)
        db.flush()

        offer = StakeOffer(
            tournament_id=tournament.id,
            player_id=user.id,
            markup=markup_value,
            total_disponivel_pct=total_pct_value,
            vendido_pct=Decimal("0"),
            escrow_status="COLLECTING",
        )
        db.add(offer)
        db.flush()

        total_required = ((buyin_value * markup_value) * total_pct_value) / Decimal("100")
        db.add(
            TournamentEscrow(
                tournament_id=tournament.id,
                offer_id=offer.id,
                total_required=q_money(total_required),
                total_collected=Decimal("0"),
                status="COLLECTING",
                deadline_at=data_hora,
            )
        )

    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.post("/api/tournaments/{tournament_id}/close")
async def close_tournament(tournament_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para continuar.")
    ensure_admin_user(user)

    payload = await request.json()
    prize_raw = payload.get("prize_amount")
    if prize_raw is None:
        raise HTTPException(status_code=400, detail="Campo prize_amount é obrigatório.")

    try:
        prize_amount = q_money(Decimal(str(prize_raw)))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor de prêmio inválido.")
    if prize_amount < 0:
        raise HTTPException(status_code=400, detail="Valor de prêmio não pode ser negativo.")

    total_allocated = Decimal("0")
    updated_count = 0
    with db.begin():
        tournament_stmt = select(Tournament).where(Tournament.id == tournament_id).with_for_update()
        tournament = db.execute(tournament_stmt).scalars().first()
        if not tournament:
            raise HTTPException(status_code=404, detail="Torneio não encontrado.")

        investment_stmt = (
            select(Investment)
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(StakeOffer.tournament_id == tournament_id)
            .with_for_update()
        )
        investments = db.execute(investment_stmt).scalars().all()
        for inv in investments:
            pct = Decimal(str(inv.pct_comprada or 0))
            valor_receber = q_money(prize_amount * pct / Decimal("100"))
            inv.lucro_recebido = valor_receber
            inv.payout_status = "PENDING"
            total_allocated += valor_receber
            updated_count += 1

        tournament.status = "Finalizado"

    return {
        "success": True,
        "tournament_id": tournament_id,
        "tournament_status": "Finalizado",
        "prize_amount": float(prize_amount),
        "allocated_amount": float(q_money(total_allocated)),
        "investments_updated": updated_count,
    }


@router.post("/api/investments/{investment_id}/mark-paid")
def mark_investment_paid(investment_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para continuar.")
    ensure_admin_user(user)

    with db.begin():
        stmt = select(Investment).where(Investment.id == investment_id).with_for_update()
        investment = db.execute(stmt).scalars().first()
        if not investment:
            raise HTTPException(status_code=404, detail="Investment não encontrado.")
        investment.payout_status = "PAID"

    return {"success": True, "investment_id": investment_id, "payout_status": "PAID"}


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
