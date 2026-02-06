from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Investment, StakeOffer, Tournament, User
from routers.auth import fetch_current_user

templates = Jinja2Templates(directory="templates")
router = APIRouter()

DEFAULT_AVATAR = "https://i.pravatar.cc/120?img=48"


def serialize_offer(offer: StakeOffer) -> dict:
    tournament = offer.tournament
    player = offer.player
    return {
        "id": offer.id,
        "player_name": player.nome if player else "Player",
        "player_avatar": DEFAULT_AVATAR,
        "tournament_name": tournament.nome if tournament else "Torneio",
        "room": tournament.sharkscope_id if tournament and tournament.sharkscope_id else "Sala não informada",
        "buyin": tournament.buyin if tournament else 0,
        "markup": offer.markup,
        "total_pct": offer.total_disponivel_pct,
        "sold_pct": offer.vendido_pct,
        "start_time": tournament.data_hora if tournament else None,
    }


@router.get("/", response_class=HTMLResponse)
def marketplace(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    wallet_summary = None
    if user and user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    stmt = (
        select(StakeOffer)
        .options(joinedload(StakeOffer.player), joinedload(StakeOffer.tournament))
        .order_by(StakeOffer.id.desc())
    )
    offers = [serialize_offer(item) for item in db.execute(stmt).scalars().all()]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "offers": offers,
            "user": user,
            "wallet": wallet_summary,
        },
    )


@router.get("/stake/{offer_id}", response_class=HTMLResponse)
def stake_detail(request: Request, offer_id: int, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    wallet_summary = None
    if user and user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id)
        .options(joinedload(StakeOffer.player), joinedload(StakeOffer.tournament))
    )
    offer = db.execute(stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    return templates.TemplateResponse(
        "stake_detail.html",
        {
            "request": request,
            "offer": serialize_offer(offer),
            "user": user,
            "wallet": wallet_summary,
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    wallet = {"saldo_disponivel": 0, "saldo_em_jogo": 0}
    if user and user.wallet:
        wallet = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }

    investments = []
    if user:
        stmt = (
            select(Investment)
            .where(Investment.backer_id == user.id)
            .options(joinedload(Investment.offer).joinedload(StakeOffer.tournament))
            .options(joinedload(Investment.offer).joinedload(StakeOffer.player))
        )
        investments = db.execute(stmt).scalars().all()

    stakes = []
    for investment in investments:
        offer = investment.offer
        tournament = offer.tournament if offer else None
        player = offer.player if offer else None
        stakes.append(
            {
                "tournament": tournament.nome if tournament else "Torneio",
                "player": player.nome if player else "Player",
                "valor": investment.valor_investido,
                "pct": investment.pct_comprada,
                "resultado": investment.lucro_recebido,
                "status": tournament.status if tournament else "Aberto",
            }
        )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "wallet": wallet,
            "stakes": stakes,
            "user": user,
            "requires_auth": True,
        },
    )


@router.get("/player/offers", response_class=HTMLResponse)
def player_offers(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "jogador":
        return RedirectResponse(url="/", status_code=303)

    wallet_summary = None
    if user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    stmt = (
        select(StakeOffer)
        .where(StakeOffer.player_id == user.id)
        .options(joinedload(StakeOffer.tournament))
        .order_by(StakeOffer.id.desc())
    )
    offers = db.execute(stmt).scalars().all()
    return templates.TemplateResponse(
        "player_offers.html",
        {
            "request": request,
            "user": user,
            "offers": offers,
            "wallet": wallet_summary,
            "requires_auth": True,
        },
    )


@router.post("/player/offers")
def create_player_offer(
    request: Request,
    tournament_name: str = Form(...),
    room: str = Form(...),
    buyin: float = Form(...),
    start_time: str = Form(""),
    markup: float = Form(...),
    total_pct: float = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "jogador":
        return RedirectResponse(url="/", status_code=303)

    try:
        buyin_value = Decimal(str(buyin))
        markup_value = Decimal(str(markup))
        total_pct_value = Decimal(str(total_pct))
    except InvalidOperation:
        return RedirectResponse(url="/player/offers", status_code=303)

    data_hora = None
    if start_time:
        try:
            data_hora = datetime.fromisoformat(start_time)
        except ValueError:
            data_hora = None

    with db.begin():
        tournament = Tournament(
            nome=tournament_name,
            sharkscope_id=room,
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
        )
        db.add(offer)

    return RedirectResponse(url="/player/offers", status_code=303)


@router.post("/api/invest")
async def invest(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    offer_id = payload.get("offer_id")
    amount_raw = payload.get("amount")
    if offer_id is None or amount_raw is None:
        raise HTTPException(status_code=400, detail="Dados inválidos.")

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor inválido.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor deve ser maior que zero.")

    with db.begin():
        user = fetch_current_user(request, db)
        if not user:
            raise HTTPException(status_code=401, detail="Faça login para investir.")
        if not user.wallet:
            raise HTTPException(status_code=400, detail="Carteira não encontrada.")

        offer_stmt = (
            select(StakeOffer)
            .where(StakeOffer.id == offer_id)
            .options(joinedload(StakeOffer.tournament))
            .with_for_update()
        )
        offer = db.execute(offer_stmt).scalars().first()
        if not offer or not offer.tournament:
            raise HTTPException(status_code=404, detail="Oferta não encontrada.")

        buyin = Decimal(str(offer.tournament.buyin))
        markup = Decimal(str(offer.markup))
        total_pct = Decimal(str(offer.total_disponivel_pct))
        sold_pct = Decimal(str(offer.vendido_pct))
        available_pct = total_pct - sold_pct

        if buyin <= 0:
            raise HTTPException(status_code=400, detail="Buy-in inválido.")

        share_pct = (amount / (buyin * markup)) * Decimal("100")
        if share_pct > available_pct:
            raise HTTPException(status_code=400, detail="Cota indisponível para este valor.")

        if user.wallet.saldo_disponivel < amount:
            raise HTTPException(status_code=400, detail="Saldo insuficiente.")

        user.wallet.saldo_disponivel = user.wallet.saldo_disponivel - amount
        user.wallet.saldo_em_jogo = user.wallet.saldo_em_jogo + amount
        offer.vendido_pct = sold_pct + share_pct
        investment = Investment(
            offer_id=offer.id,
            backer_id=user.id,
            valor_investido=amount,
            pct_comprada=share_pct,
            lucro_recebido=Decimal("0"),
        )
        db.add(investment)

    return {"success": True}
