from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import CallSchedule, MatchResult, StakeOffer, Tournament, Wallet, WithdrawalRequest
from routers.auth import fetch_current_user, get_wallet_summary, is_user_kyc_approved
from services.notifications import notify_all_admins
from services.storage import save_match_file

templates = Jinja2Templates(directory="templates")
router = APIRouter()


@router.get("/player/results/new", response_class=HTMLResponse)
def player_result_form(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "jogador":
        return RedirectResponse(url="/dashboard", status_code=303)

    stmt = (
        select(StakeOffer)
        .where(StakeOffer.player_id == user.id)
        .options(joinedload(StakeOffer.tournament))
        .order_by(StakeOffer.id.desc())
    )
    offers = db.execute(stmt).scalars().all()
    result_stmt = (
        select(MatchResult)
        .where(MatchResult.player_id == user.id)
        .order_by(MatchResult.submitted_at.desc())
    )
    match_results = db.execute(result_stmt).scalars().all()
    result_by_tournament = {r.tournament_id: r for r in match_results}
    offers_with_result = [(o, result_by_tournament[o.tournament.id]) for o in offers if o.tournament.id in result_by_tournament]
    return templates.TemplateResponse(
        "player_submit_result.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "offers": offers,
            "result_by_tournament": result_by_tournament,
            "offers_with_result": offers_with_result,
            "error": None,
            "requires_auth": True,
        },
    )


@router.post("/player/results", response_class=HTMLResponse)
async def submit_player_result(
    request: Request,
    tournament_id: int = Form(...),
    posicao_final: int = Form(...),
    valor_premio: float = Form(...),
    valor_enviado: float = Form(...),
    resultado_print: UploadFile = File(...),
    comprovante_pagamento: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "jogador":
        return RedirectResponse(url="/dashboard", status_code=303)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin.")

    stmt = (
        select(StakeOffer)
        .where(StakeOffer.player_id == user.id, StakeOffer.tournament_id == tournament_id)
        .options(joinedload(StakeOffer.tournament))
    )
    offer = db.execute(stmt).scalars().first()
    if not offer or not offer.tournament:
        raise HTTPException(status_code=404, detail="Não encontramos esta oferta/torneio para o seu perfil.")

    try:
        premio_value = Decimal(str(valor_premio))
        enviado_value = Decimal(str(valor_enviado))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Valores financeiros inválidos. Revise os campos e tente novamente.") from exc

    if posicao_final <= 0:
        raise HTTPException(status_code=400, detail="Posição final inválida. Informe uma posição maior que zero.")
    if premio_value < 0 or enviado_value < 0:
        raise HTTPException(status_code=400, detail="Os valores informados devem ser positivos.")

    try:
        print_url = await save_match_file(resultado_print, user_id=user.id, kind="print")
        comprovante_url = await save_match_file(comprovante_pagamento, user_id=user.id, kind="comprovante")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result_stmt = select(MatchResult).where(
        MatchResult.tournament_id == tournament_id,
        MatchResult.player_id == user.id,
    )
    existing_result = db.execute(result_stmt).scalars().first()
    if existing_result:
        if existing_result.review_status == "APPROVED":
            raise HTTPException(
                status_code=400,
                detail="Resultado já aprovado pelo admin e não pode mais ser alterado.",
            )
        existing_result.posicao_final = posicao_final
        existing_result.valor_premio = premio_value
        existing_result.valor_enviado = enviado_value
        existing_result.print_url = print_url
        existing_result.comprovante_url = comprovante_url
        existing_result.review_status = "UNDER_REVIEW"
        existing_result.admin_verified = False
    else:
        db.add(
            MatchResult(
                tournament_id=tournament_id,
                player_id=user.id,
                posicao_final=posicao_final,
                valor_premio=premio_value,
                valor_enviado=enviado_value,
                print_url=print_url,
                comprovante_url=comprovante_url,
                review_status="UNDER_REVIEW",
                admin_verified=False,
            )
        )
    if user.is_blocked and user.blocked_reason and "resultado" in user.blocked_reason.lower():
        user.is_blocked = False
        user.blocked_reason = None
        user.blocked_at = None
    notify_all_admins(
        db,
        n_type="RESULT_SUBMITTED",
        title="Novo resultado em revisão",
        message=(
            f"{user.nome} enviou/atualizou resultado do torneio #{offer.tournament_id} "
            f"com valor enviado de US$ {enviado_value:.2f}."
        ),
        action_url="/admin/results",
    )
    db.commit()
    return RedirectResponse(url="/player/results/new?sent=1", status_code=303)


@router.get("/player/calls", response_class=HTMLResponse)
def player_call_schedule(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    stmt = (
        select(CallSchedule)
        .where(CallSchedule.user_id == user.id)
        .order_by(CallSchedule.created_at.desc(), CallSchedule.id.desc())
    )
    schedules = db.execute(stmt).scalars().all()
    return templates.TemplateResponse(
        "schedule_call.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "schedules": schedules,
            "requires_auth": True,
        },
    )


@router.post("/player/calls")
def create_player_call_schedule(
    request: Request,
    scheduled_at: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    try:
        scheduled_dt = datetime.fromisoformat(scheduled_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Data/hora inválida para agendamento. Escolha uma data válida.") from exc
    if scheduled_dt <= datetime.now(scheduled_dt.tzinfo):
        raise HTTPException(status_code=400, detail="Escolha uma data/hora futura para o agendamento.")

    db.add(
        CallSchedule(
            user_id=user.id,
            scheduled_at=scheduled_dt,
            status="PENDING",
            notes=notes.strip()[:255] if notes else None,
        )
    )
    db.commit()
    return RedirectResponse(url="/player/calls?created=1", status_code=303)


@router.post("/player/withdrawals/request")
def request_withdrawal(
    request: Request,
    amount: float = Form(...),
    pix_key: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação para solicitar saque.")

    try:
        amount_value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Valor de saque inválido. Informe um número válido.") from exc
    if amount_value <= 0:
        raise HTTPException(status_code=400, detail="Valor de saque deve ser maior que zero.")

    wallet = db.execute(select(Wallet).where(Wallet.user_id == user.id)).scalars().first()
    if not wallet:
        raise HTTPException(status_code=400, detail="Carteira não encontrada. Atualize a página e tente novamente.")
    saldo_disp = Decimal(str(wallet.saldo_disponivel or 0))
    if amount_value > saldo_disp:
        raise HTTPException(status_code=400, detail="Saldo insuficiente para esta solicitação de saque.")

    destination_pix = (pix_key or "").strip() or (user.pix_key or "")
    if not destination_pix:
        raise HTTPException(status_code=400, detail="Informe uma chave PIX de destino para o saque.")

    db.add(
        WithdrawalRequest(
            user_id=user.id,
            amount=amount_value,
            pix_key=destination_pix,
            status="PENDING",
        )
    )
    notify_all_admins(
        db,
        n_type="WITHDRAWAL_REQUESTED",
        title="Nova solicitação de saque",
        message=(
            f"Apoiador/Jogador: {user.nome} | Valor: US$ {amount_value:.2f} | "
            f"PIX destino: {destination_pix}"
        ),
        action_url="/admin/dashboard",
    )
    db.commit()
    return RedirectResponse(url="/dashboard?withdraw_requested=1", status_code=303)
