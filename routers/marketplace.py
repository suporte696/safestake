from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from constants import normalize_supported_room
from db import get_db
from models import Investment, PixTransaction, StakeBid, StakeOffer, Tournament, TournamentEscrow, User, Wallet
from routers.auth import ensure_user_not_blocked, fetch_current_user, is_user_kyc_approved
from routers.escrow import sync_offer_escrow
from services.jobs import run_scheduled_jobs

templates = Jinja2Templates(directory="templates")
router = APIRouter()

DEFAULT_AVATAR = "/static/img/safestake-icon.png"
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _is_offer_closed_by_start_time(offer: StakeOffer) -> bool:
    """
    Considera a oferta encerrada somente a partir do horário de início do torneio
    na hora de São Paulo, comparando sempre em UTC para evitar problemas de fuso.
    """
    tournament = offer.tournament
    if not tournament or not tournament.data_hora:
        return False

    start_at = tournament.data_hora
    if start_at.tzinfo is None:
        # Datas antigas ou salvas sem timezone: assumimos que foram cadastradas em horário local (São Paulo)
        start_at = start_at.replace(tzinfo=LOCAL_TZ)

    start_utc = start_at.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    return now_utc >= start_utc


def serialize_offer(offer: StakeOffer) -> dict:
    tournament = offer.tournament
    player = offer.player
    total_pct = Decimal(str(offer.total_disponivel_pct or 0))
    sold_pct = Decimal(str(offer.vendido_pct or 0))
    available_pct = total_pct - sold_pct
    is_closed = _is_offer_closed_by_start_time(offer)
    can_support = offer.escrow_status == "COLLECTING" and available_pct > 0 and not is_closed
    return {
        "id": offer.id,
        "player_name": player.nome if player else "Player",
        "player_avatar": DEFAULT_AVATAR,
        "tournament_name": tournament.nome if tournament else "Torneio",
        "room": (
            tournament.plataforma
            if tournament and getattr(tournament, "plataforma", None)
            else (tournament.sharkscope_id if tournament and tournament.sharkscope_id else "Sala não informada")
        ),
        "buyin": tournament.buyin if tournament else 0,
        "markup": offer.markup,
        "total_pct": total_pct,
        "sold_pct": sold_pct,
        "available_pct": available_pct,
        "start_time": tournament.data_hora if tournament else None,
        "is_closed": is_closed,
        "can_support": can_support,
    }


@router.get("/", response_class=HTMLResponse)
def marketplace(request: Request, db: Session = Depends(get_db)):
    with db.begin():
        run_scheduled_jobs(db)
    user = fetch_current_user(request, db)
    wallet_summary = None
    if user and user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    stmt = (
        select(StakeOffer)
        .join(Tournament, StakeOffer.tournament_id == Tournament.id)
        .where(Tournament.status.in_(("Aberto", "Jogando")))
        .where(StakeOffer.escrow_status == "COLLECTING")
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
    with db.begin():
        run_scheduled_jobs(db)
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
    total_investido = Decimal("0")
    total_recebido = Decimal("0")
    for investment in investments:
        offer = investment.offer
        tournament = offer.tournament if offer else None
        player = offer.player if offer else None
        valor_investido = Decimal(str(investment.valor_investido or 0))
        lucro_recebido = Decimal(str(investment.lucro_recebido or 0))
        total_investido += valor_investido
        total_recebido += lucro_recebido
        stakes.append(
            {
                "tournament": tournament.nome if tournament else "Torneio",
                "player": player.nome if player else "Player",
                "valor": valor_investido,
                "pct": investment.pct_comprada,
                "resultado": lucro_recebido,
                "status": tournament.status if tournament else "Aberto",
            }
        )

    pix_transactions = []
    if user:
        stmt_tx = (
            select(PixTransaction)
            .where(PixTransaction.user_id == user.id)
            .order_by(PixTransaction.created_at.desc(), PixTransaction.id.desc())
            .limit(10)
        )
        raw_txs = db.execute(stmt_tx).scalars().all()
        for tx in raw_txs:
            dt = tx.created_at
            if not dt:
                continue
            # Garantimos que o datetime está com timezone e convertemos para horário de São Paulo
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tx.created_at_local = dt.astimezone(LOCAL_TZ)
        pix_transactions = raw_txs

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
            "pix_transactions": pix_transactions,
            "total_investido": total_investido,
            "total_recebido": total_recebido,
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
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para criar ofertas.")
    normalized_room = normalize_supported_room(room)
    if not normalized_room:
        raise HTTPException(status_code=400, detail="Sala/plataforma não suportada pelo SharkScope.")

    try:
        buyin_value = Decimal(str(buyin))
        markup_value = Decimal(str(markup))
        total_pct_value = Decimal(str(total_pct))
    except InvalidOperation:
        return RedirectResponse(url="/player/offers", status_code=303)

    data_hora = None
    if start_time:
        try:
            # Valor vindo de <input type="datetime-local"> (sem timezone).
            local_dt = datetime.fromisoformat(start_time)
            if local_dt.tzinfo is None:
                local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
            data_hora = local_dt
        except ValueError:
            data_hora = None

    with db.begin():
        tournament = Tournament(
            nome=tournament_name,
            sharkscope_id=normalized_room,
            plataforma=normalized_room,
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
        deadline_at = data_hora or (datetime.now(timezone.utc) + timedelta(hours=24))
        db.add(
            TournamentEscrow(
                tournament_id=tournament.id,
                offer_id=offer.id,
                total_required=total_required,
                total_collected=Decimal("0"),
                status="COLLECTING",
                deadline_at=deadline_at,
            )
        )

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
        ensure_user_not_blocked(user)
        if not is_user_kyc_approved(user, db):
            raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para investir.")
        if not user.wallet:
            raise HTTPException(status_code=400, detail="Carteira não encontrada.")

        offer_stmt = (
            select(StakeOffer)
            .where(StakeOffer.id == offer_id)
            .with_for_update()
        )
        offer = db.execute(offer_stmt).scalars().first()
        if not offer:
            raise HTTPException(status_code=404, detail="Oferta não encontrada.")
        tournament = db.execute(select(Tournament).where(Tournament.id == offer.tournament_id)).scalars().first()
        if not tournament:
            raise HTTPException(status_code=404, detail="Torneio da oferta não encontrado.")
        if offer.escrow_status != "COLLECTING":
            raise HTTPException(status_code=400, detail="Escrow desta oferta não está mais aberto para investimento.")
        if _is_offer_closed_by_start_time(offer):
            raise HTTPException(status_code=400, detail="Esta oferta está encerrada para apoio.")

        buyin = Decimal(str(tournament.buyin))
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
        sync_offer_escrow(db, offer)

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
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para enviar propostas.")

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
        if offer.escrow_status != "COLLECTING":
            raise HTTPException(status_code=400, detail="Escrow desta oferta não está mais aberto para propostas.")
        if _is_offer_closed_by_start_time(offer):
            raise HTTPException(status_code=400, detail="Esta oferta está encerrada para novas propostas.")
        buyin = Decimal(str(offer.tournament.buyin))
        if buyin <= 0:
            raise HTTPException(status_code=400, detail="Buy-in inválido para receber propostas.")
        total_pct = Decimal(str(offer.total_disponivel_pct))
        sold_pct = Decimal(str(offer.vendido_pct))
        available_pct = total_pct - sold_pct
        share_pct = (amount / (buyin * proposed_markup)) * Decimal("100")
        if share_pct > available_pct:
            raise HTTPException(status_code=400, detail="Percentual disponível insuficiente para esta proposta.")

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
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para responder propostas.")

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
        if offer.escrow_status != "COLLECTING":
            raise HTTPException(status_code=400, detail="Escrow desta oferta não está mais aberto para propostas.")

        backer_wallet_stmt = select(Wallet).where(Wallet.user_id == bid.backer_id).with_for_update()
        backer_wallet = db.execute(backer_wallet_stmt).scalars().first()
        if not backer_wallet:
            raise HTTPException(status_code=400, detail="Carteira do apoiador não encontrada.")

        amount = Decimal(str(bid.amount))
        saldo_bloq = Decimal(str(getattr(backer_wallet, "saldo_bloqueado", 0) or 0))
        if saldo_bloq < amount:
            bid.status = "REJECTED"
            raise HTTPException(status_code=400, detail="Saldo bloqueado insuficiente para processar a proposta.")

        if action == "ACCEPT" and _is_offer_closed_by_start_time(offer):
            backer_wallet.saldo_bloqueado = saldo_bloq - amount
            backer_wallet.saldo_disponivel = Decimal(str(backer_wallet.saldo_disponivel)) + amount
            bid.status = "REJECTED"
            raise HTTPException(status_code=400, detail="Esta oferta está encerrada e não aceita novas confirmações.")

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
        sync_offer_escrow(db, offer)

    return {"success": True, "status": "ACCEPTED"}
