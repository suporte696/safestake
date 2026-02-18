from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import CallSchedule, MatchResult, StakeOffer, Tournament
from routers.auth import fetch_current_user, get_wallet_summary, is_user_kyc_approved
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
    return templates.TemplateResponse(
        "player_submit_result.html",
        {
            "request": request,
            "user": user,
            "wallet": get_wallet_summary(user),
            "offers": offers,
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
        raise HTTPException(status_code=404, detail="Torneio não encontrado para este jogador.")

    try:
        premio_value = Decimal(str(valor_premio))
        enviado_value = Decimal(str(valor_enviado))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Valores financeiros inválidos.") from exc

    if posicao_final <= 0:
        raise HTTPException(status_code=400, detail="Posição final inválida.")
    if premio_value < 0 or enviado_value < 0:
        raise HTTPException(status_code=400, detail="Valores devem ser positivos.")

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
        existing_result.posicao_final = posicao_final
        existing_result.valor_premio = premio_value
        existing_result.valor_enviado = enviado_value
        existing_result.print_url = print_url
        existing_result.comprovante_url = comprovante_url
        existing_result.review_status = "PENDING"
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
                review_status="PENDING",
                admin_verified=False,
            )
        )
    if user.is_blocked and user.blocked_reason and "resultado" in user.blocked_reason.lower():
        user.is_blocked = False
        user.blocked_reason = None
        user.blocked_at = None
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
        raise HTTPException(status_code=400, detail="Data/hora inválida para agendamento.") from exc
    if scheduled_dt <= datetime.now(scheduled_dt.tzinfo):
        raise HTTPException(status_code=400, detail="Escolha uma data/hora futura.")

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
