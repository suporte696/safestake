from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from db import get_db
from models import Investment, StakeBid, StakeOffer, Tournament, User, Wallet
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


def serialize_bid(bid: StakeBid) -> dict:
    offer = bid.offer
    tournament = offer.tournament if offer else None
    backer = bid.backer
    return {
        "id": bid.id,
        "offer_id": bid.offer_id,
        "tournament_name": tournament.nome if tournament else "Torneio",
        "backer_name": backer.nome if backer else "Apoiador",
        "amount": bid.amount,
        "proposed_markup": bid.proposed_markup,
        "status": bid.status,
        "created_at": bid.created_at,
    }


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    wallet = {"saldo_disponivel": 0, "saldo_bloqueado": 0, "saldo_em_jogo": 0}
    if user and user.wallet:
        wallet = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_bloqueado": getattr(user.wallet, "saldo_bloqueado", Decimal("0")) or Decimal("0"),
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

    bids_received = []
    my_bids = []
    if user.tipo == "jogador":
        stmt_bids_received = (
            select(StakeBid)
            .join(StakeOffer, StakeBid.offer_id == StakeOffer.id)
            .where(StakeOffer.player_id == user.id)
            .where(StakeBid.status == "PENDING")
            .options(joinedload(StakeBid.offer).joinedload(StakeOffer.tournament))
            .options(joinedload(StakeBid.backer))
            .order_by(StakeBid.created_at.desc())
        )
        for b in db.execute(stmt_bids_received).scalars().all():
            bids_received.append(serialize_bid(b))
    if user.tipo == "apoiador":
        stmt_my_bids = (
            select(StakeBid)
            .where(StakeBid.backer_id == user.id)
            .options(joinedload(StakeBid.offer).joinedload(StakeOffer.tournament))
            .order_by(StakeBid.created_at.desc())
        )
        for b in db.execute(stmt_my_bids).scalars().all():
            my_bids.append(serialize_bid(b))

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "wallet": wallet,
            "stakes": stakes,
            "bids_received": bids_received,
            "my_bids": my_bids,
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


# --- Bid (Contraproposta de Markup) ---

MIN_PROPOSED_MARKUP = Decimal("0.5")


@router.post("/api/bid/create")
async def bid_create(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    offer_id = payload.get("offer_id")
    amount_raw = payload.get("amount")
    proposed_markup_raw = payload.get("proposed_markup")

    if offer_id is None or amount_raw is None or proposed_markup_raw is None:
        raise HTTPException(status_code=400, detail="Dados inválidos: offer_id, amount e proposed_markup obrigatórios.")

    try:
        amount = Decimal(str(amount_raw))
        proposed_markup = Decimal(str(proposed_markup_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor ou markup inválido.")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor deve ser maior que zero.")
    if proposed_markup < MIN_PROPOSED_MARKUP:
        raise HTTPException(status_code=400, detail=f"Markup proposto deve ser >= {MIN_PROPOSED_MARKUP}.")

    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para enviar proposta.")
    if user.tipo != "apoiador":
        raise HTTPException(status_code=403, detail="Apenas apoiadores podem enviar bids.")

    with db.begin():
        wallet_stmt = select(Wallet).where(Wallet.user_id == user.id).with_for_update()
        wallet = db.execute(wallet_stmt).scalars().first()
        if not wallet:
            raise HTTPException(status_code=400, detail="Carteira não encontrada.")

        offer_stmt = (
            select(StakeOffer)
            .where(StakeOffer.id == offer_id)
            .options(joinedload(StakeOffer.tournament))
        )
        offer = db.execute(offer_stmt).scalars().first()
        if not offer or not offer.tournament:
            raise HTTPException(status_code=404, detail="Oferta não encontrada.")

        saldo_disp = Decimal(str(wallet.saldo_disponivel))
        if saldo_disp < amount:
            raise HTTPException(status_code=400, detail="Saldo disponível insuficiente.")

        saldo_bloq = Decimal(str(getattr(wallet, "saldo_bloqueado", 0) or 0))
        wallet.saldo_disponivel = saldo_disp - amount
        wallet.saldo_bloqueado = saldo_bloq + amount
        bid = StakeBid(
            offer_id=offer.id,
            backer_id=user.id,
            amount=amount,
            proposed_markup=proposed_markup,
            status="PENDING",
        )
        db.add(bid)
        db.flush()

    return {"success": True, "bid_id": bid.id}


@router.post("/api/bid/{bid_id}/respond")
async def bid_respond(
    request: Request,
    bid_id: int,
    db: Session = Depends(get_db),
):
    payload = await request.json()
    action = payload.get("action")
    if action not in ("ACCEPT", "REJECT"):
        raise HTTPException(status_code=400, detail="action deve ser ACCEPT ou REJECT.")

    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login.")
    if user.tipo != "jogador":
        raise HTTPException(status_code=403, detail="Apenas o dono da oferta pode responder.")

    with db.begin():
        bid_stmt = (
            select(StakeBid)
            .where(StakeBid.id == bid_id)
            .options(
                joinedload(StakeBid.offer).joinedload(StakeOffer.tournament),
                joinedload(StakeBid.offer).joinedload(StakeOffer.player),
                joinedload(StakeBid.backer),
            )
        )
        bid = db.execute(bid_stmt).scalars().first()
        if not bid:
            raise HTTPException(status_code=404, detail="Proposta não encontrada.")
        if bid.status != "PENDING":
            raise HTTPException(status_code=400, detail="Esta proposta já foi respondida.")

        offer = bid.offer
        if offer.player_id != user.id:
            raise HTTPException(status_code=403, detail="Apenas o dono da oferta pode responder.")

        backer_wallet_stmt = select(Wallet).where(Wallet.user_id == bid.backer_id).with_for_update()
        backer_wallet = db.execute(backer_wallet_stmt).scalars().first()
        if not backer_wallet:
            raise HTTPException(status_code=400, detail="Carteira do apoiador não encontrada.")

        amount = Decimal(str(bid.amount))
        saldo_bloq = Decimal(str(getattr(backer_wallet, "saldo_bloqueado", 0) or 0))
        if saldo_bloq < amount:
            bid.status = "CANCELLED"
            raise HTTPException(status_code=400, detail="Saldo bloqueado insuficiente; proposta cancelada.")

        if action == "REJECT":
            backer_wallet.saldo_bloqueado = saldo_bloq - amount
            backer_wallet.saldo_disponivel = Decimal(str(backer_wallet.saldo_disponivel)) + amount
            bid.status = "REJECTED"
            return {"success": True, "status": "REJECTED"}

        # ACCEPT
        buyin = Decimal(str(offer.tournament.buyin))
        total_pct = Decimal(str(offer.total_disponivel_pct))
        sold_pct = Decimal(str(offer.vendido_pct))
        available_pct = total_pct - sold_pct
        proposed_markup = Decimal(str(bid.proposed_markup))
        if buyin <= 0 or proposed_markup <= 0:
            raise HTTPException(status_code=400, detail="Dados da oferta inválidos.")

        share_pct = (amount / (buyin * proposed_markup)) * Decimal("100")
        if share_pct > available_pct:
            backer_wallet.saldo_bloqueado = saldo_bloq - amount
            backer_wallet.saldo_disponivel = Decimal(str(backer_wallet.saldo_disponivel)) + amount
            bid.status = "REJECTED"
            raise HTTPException(
                status_code=400,
                detail="Percentual disponível na oferta não é mais suficiente para este valor/markup.",
            )

        backer_wallet.saldo_bloqueado = saldo_bloq - amount
        backer_wallet.saldo_em_jogo = Decimal(str(backer_wallet.saldo_em_jogo)) + amount
        offer.vendido_pct = sold_pct + share_pct
        investment = Investment(
            offer_id=offer.id,
            backer_id=bid.backer_id,
            valor_investido=amount,
            pct_comprada=share_pct,
            lucro_recebido=Decimal("0"),
        )
        db.add(investment)
        bid.status = "ACCEPTED"

    return {"success": True, "status": "ACCEPTED"}
