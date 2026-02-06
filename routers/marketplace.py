from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Investment, StakeOffer, User

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
    stmt = (
        select(StakeOffer)
        .options(joinedload(StakeOffer.player), joinedload(StakeOffer.tournament))
        .order_by(StakeOffer.id.desc())
    )
    offers = [serialize_offer(item) for item in db.execute(stmt).scalars().all()]
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "offers": offers},
    )


@router.get("/stake/{offer_id}", response_class=HTMLResponse)
def stake_detail(request: Request, offer_id: int, db: Session = Depends(get_db)):
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
        {"request": request, "offer": serialize_offer(offer)},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.tipo == "apoiador")).scalars().first()
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
        {"request": request, "wallet": wallet, "stakes": stakes},
    )
