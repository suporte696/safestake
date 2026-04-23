from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session, joinedload

from constants import SUPPORTED_ROOMS, normalize_supported_room
from db import get_db
from models import Investment, MatchResult, PixTransaction, StakeBid, StakeOffer, Tournament, TournamentEscrow, User, Wallet, WithdrawalRequest
from routers.auth import ensure_user_not_blocked, fetch_current_user, is_user_kyc_approved
from routers.escrow import (
    force_complete_and_release_escrow,
    q_money,
    q_pct,
    refund_offer_escrow,
    release_offer_escrow_to_player,
    sync_offer_escrow,
)
from services.jobs import run_scheduled_jobs

templates = Jinja2Templates(directory="templates")
router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_AVATAR = "/static/img/avatar-fallback.svg"
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def ensure_wallet_for_user(db: Session, user_id: int, with_lock: bool = False) -> Wallet:
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


def _is_offer_closed_by_start_time(offer: StakeOffer) -> bool:
    """
    Considera a oferta encerrada somente a partir do horário de início do torneio.

    Importante: sempre interpretamos o horário salvo em `data_hora` como
    horário de São Paulo, independente do timezone que veio do banco.
    Assim, tanto registros antigos (salvos em UTC) quanto novos (salvos com
    timezone de São Paulo) passam a se comportar corretamente.
    """
    tournament = offer.tournament
    if not tournament or not tournament.data_hora:
        return False

    start_at = tournament.data_hora
    # data_hora é sempre naive em horário de São Paulo
    start_local = start_at.replace(tzinfo=LOCAL_TZ) if start_at.tzinfo is None else start_at.astimezone(LOCAL_TZ)
    start_utc = start_local.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    return now_utc >= start_utc


def serialize_offer(offer: StakeOffer) -> dict:
    tournament = offer.tournament
    player = offer.player
    total_pct = Decimal(str(offer.total_disponivel_pct or 0))
    sold_pct = Decimal(str(offer.vendido_pct or 0))
    available_pct = total_pct - sold_pct
    buyin = Decimal(str(tournament.buyin if tournament else 0))
    sold_buyin_amount = (buyin * sold_pct) / Decimal("100")
    target_buyin_amount = (buyin * total_pct) / Decimal("100")
    progress_sale_pct = Decimal("0")
    if target_buyin_amount > 0:
        progress_sale_pct = min(Decimal("100"), (sold_buyin_amount / target_buyin_amount) * Decimal("100"))
    is_closed = _is_offer_closed_by_start_time(offer)
    
    # Lógica de Status Centralizada
    status_label = "Ativa"
    status_class = "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
    
    if tournament and tournament.status == "Finalizado":
        status_label = "Finalizada"
        status_class = "bg-gray-500/10 border-gray-500/20 text-gray-400"
    elif tournament and tournament.status == "Jogando":
        status_label = "Iniciada"
        status_class = "bg-emerald-500/10 border-emerald-500/20 text-emerald-400"
    elif is_closed:
        status_label = "Encerrada"
        status_class = "bg-red-500/10 border-red-500/20 text-red-400"
    elif progress_sale_pct >= 100:
        status_label = "Meta Atingida"
        status_class = "bg-amber-500/10 border-amber-500/20 text-amber-400"

    can_support = offer.escrow_status == "COLLECTING" and available_pct > 0 and not is_closed
    start_time = tournament.data_hora if tournament else None
    
    avatar_url = player.profile_photo_url if player and player.profile_photo_url else DEFAULT_AVATAR
    if avatar_url and not avatar_url.startswith("/") and not avatar_url.startswith("http"):
        # Provavelmente caminho legado apenas com o nome do arquivo
        avatar_url = f"/static/uploads/profile/{avatar_url}"
    
    return {
        "id": offer.id,
        "player_name": player.nome if player else "Player",
        "player_avatar": avatar_url,
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
        "sold_buyin_amount": sold_buyin_amount,
        "target_buyin_amount": target_buyin_amount,
        "progress_sale_pct": progress_sale_pct,
        "start_time": start_time,
        "is_closed": is_closed,
        "can_support": can_support,
        "status_label": status_label,
        "status_class": status_class,
    }


@router.get("/", response_class=HTMLResponse)
def marketplace(request: Request, db: Session = Depends(get_db)):
    try:
        run_scheduled_jobs(db)
        db.commit()
    except Exception:
        logger.exception("Falha ao executar jobs agendados no carregamento do marketplace")
        db.rollback()
    user = fetch_current_user(request, db)
    wallet_summary = None
    if user and user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    now_sp = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    cutoff_time = now_sp - timedelta(hours=12)

    stmt = (
        select(StakeOffer)
        .join(Tournament, StakeOffer.tournament_id == Tournament.id)
        .where(Tournament.status.in_(("Aberto", "Jogando")))
        .where(StakeOffer.escrow_status.in_(("COLLECTING", "COMPLETE")))
        .where(
            or_(
                Tournament.data_hora >= cutoff_time,
                Tournament.data_hora.is_(None)
            )
        )
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
    investment_data = None
    if user:
        inv_stmt = select(Investment).where(Investment.offer_id == offer_id, Investment.backer_id == user.id)
        existing_inv = db.execute(inv_stmt).scalars().first()
        if existing_inv:
            tournament = offer.tournament
            is_editable = (
                tournament.status == "Aberto" and 
                not _is_offer_closed_by_start_time(offer) and 
                offer.escrow_status in ("COLLECTING", "COMPLETE")
            )
            investment_data = {
                "id": existing_inv.id,
                "amount": existing_inv.valor_investido,
                "pct": existing_inv.pct_comprada,
                "is_editable": is_editable
            }

    return templates.TemplateResponse(
        "stake_detail.html",
        {
            "request": request,
            "offer": serialize_offer(offer),
            "user": user,
            "wallet": wallet_summary,
            "user_investment": investment_data,
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


@router.get("/view-as/admin")
def view_as_admin(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "admin":
        return RedirectResponse(url="/dashboard", status_code=303)
    request.session["active_profile"] = "admin"
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.get("/view-as/player")
def view_as_player(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo != "admin":
        return RedirectResponse(url="/dashboard", status_code=303)
    request.session["active_profile"] = "player"
    return RedirectResponse(url="/player/offers", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        run_scheduled_jobs(db)
        db.commit()
    except Exception:
        logger.exception("Falha ao executar jobs agendados no carregamento do dashboard")
        db.rollback()
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    active_profile = request.session.get("active_profile")
    if user.tipo == "admin":
        if active_profile == "player":
            return RedirectResponse(url="/player/offers", status_code=303)
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if user.tipo == "jogador":
        return RedirectResponse(url="/player/offers", status_code=303)
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
            .order_by(Investment.id.desc())
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
        lucro_bruto = Decimal(str(investment.lucro_recebido or 0))
        # Quando o torneio está finalizado e o pagamento foi efetuado, "resultado" é o total líquido
        # recebido de volta (capital investido + lucro), equivalente ao que o admin chama de "Líquido".
        # Se o payout ainda não aconteceu (PENDING), mostramos apenas o lucro estimado.
        if investment.payout_status == "PAID":
            resultado = q_money(valor_investido + lucro_bruto)
        else:
            resultado = lucro_bruto
        total_investido += valor_investido
        total_recebido += resultado if investment.payout_status == "PAID" else Decimal("0")
        status_label = tournament.status if tournament else "Aberto"
        if offer and offer.escrow_status == "REFUNDED":
            status_label = "Cancelado"
        
        is_editable = False
        if offer and tournament:
            is_editable = (
                tournament.status == "Aberto" and 
                not _is_offer_closed_by_start_time(offer) and 
                offer.escrow_status in ("COLLECTING", "COMPLETE")
            )

        stakes.append(
            {
                "id": investment.id,
                "offer_id": offer.id if offer else None,
                "tournament": tournament.nome if tournament else "Torneio",
                "player": player.nome if player else "Player",
                "valor": valor_investido,
                "pct": investment.pct_comprada,
                "resultado": resultado,
                "status": status_label,
                "is_editable": is_editable,
                "buyin": float(tournament.buyin) if tournament else 0,
                "markup": float(offer.markup) if offer else 1.0,
                "total_disponivel_pct": float(offer.total_disponivel_pct) if offer else 100,
                "vendido_pct": float(offer.vendido_pct) if offer else 0,
                "status_label": serialize_offer(investment.offer)["status_label"],
                "status_class": serialize_offer(investment.offer)["status_class"],
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
    withdrawal_requests = []
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

    stmt_withdrawals = (
        select(WithdrawalRequest)
        .where(WithdrawalRequest.user_id == user.id)
        .order_by(WithdrawalRequest.created_at.desc(), WithdrawalRequest.id.desc())
        .limit(10)
    )
    withdrawal_requests = db.execute(stmt_withdrawals).scalars().all()

    saldo_pendente = Decimal("0")
    if user and user.tipo == "apoiador":
        # Só mostra saldo pendente quando o jogador já submeteu o resultado
        # mas o admin ainda não aprovou (MatchResult em PENDING ou UNDER_REVIEW).
        pending_result_tournament_ids_subq = (
            select(MatchResult.tournament_id)
            .where(MatchResult.review_status.in_(["PENDING", "UNDER_REVIEW"]))
            .scalar_subquery()
        )
        pendente_row = db.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(Investment.valor_investido, 0) + func.coalesce(Investment.lucro_recebido, 0)
                    ),
                    0,
                )
            )
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(
                Investment.backer_id == user.id,
                Investment.payout_status == "PENDING",
                StakeOffer.tournament_id.in_(pending_result_tournament_ids_subq),
            )
        ).scalar_one()
        saldo_pendente = q_money(Decimal(str(pendente_row or 0)))
    if saldo_pendente is None:
        saldo_pendente = Decimal("0")

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "wallet": wallet,
            "stakes": stakes,
            "bids_received": bids_received,
            "my_bids": my_bids,
            "withdrawal_requests": withdrawal_requests,
            "pix_transactions": pix_transactions,
            "total_investido": total_investido,
            "total_recebido": total_recebido,
            "saldo_pendente": saldo_pendente,
            "user": user,
            "requires_auth": True,
        },
    )


@router.get("/player/offers", response_class=HTMLResponse)
def player_offers(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    # Admin tem perfil jogador implícito (nested); não é necessário tipo=jogador no banco
    if user.tipo not in ("jogador", "admin"):
        return RedirectResponse(url="/", status_code=303)

    wallet_summary = None
    if user.wallet:
        wallet_summary = {
            "saldo_disponivel": user.wallet.saldo_disponivel,
            "saldo_bloqueado": getattr(user.wallet, "saldo_bloqueado", Decimal("0")) or Decimal("0"),
            "saldo_em_jogo": user.wallet.saldo_em_jogo,
        }
    stmt = (
        select(StakeOffer)
        .where(StakeOffer.player_id == user.id)
        .options(joinedload(StakeOffer.tournament))
        .options(joinedload(StakeOffer.investments).joinedload(Investment.backer))
        .order_by(StakeOffer.id.desc())
    )
    offers = db.execute(stmt).unique().scalars().all()
    relevant_statuses = ("Jogando", "Finalizado")
    tournament_ids_needs_check = [o.tournament_id for o in offers if o.tournament and o.tournament.status in relevant_statuses]
    
    has_result_ids: set[int] = set()
    result_map: dict[int, MatchResult] = {}
    if tournament_ids_needs_check:
        results = db.execute(
            select(MatchResult).where(
                MatchResult.tournament_id.in_(tournament_ids_needs_check),
                MatchResult.player_id == user.id
            )
        ).scalars().all()
        result_map = {int(r.tournament_id): r for r in results if r.tournament_id is not None}
        has_result_ids = set(result_map.keys())
    
    awaiting_result_count = len([tid for tid in tournament_ids_needs_check if tid not in has_result_ids])
    
    # Injetar o percentual de progresso de venda em cada oferta para consistência com o Marketplace
    for o in offers:
        total_pct = Decimal(str(o.total_disponivel_pct or 0))
        sold_pct = Decimal(str(o.vendido_pct or 0))
        if total_pct > 0:
            o.progress_sale_pct = min(Decimal("100"), (sold_pct / total_pct) * Decimal("100"))
        else:
            o.progress_sale_pct = Decimal("0")

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
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tx.created_at_local = dt.astimezone(LOCAL_TZ)
        pix_transactions = raw_txs

    withdrawal_requests = []
    if user:
        stmt_withdrawals = (
            select(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == user.id)
            .order_by(WithdrawalRequest.created_at.desc(), WithdrawalRequest.id.desc())
            .limit(10)
        )
        withdrawal_requests = db.execute(stmt_withdrawals).scalars().all()

    return templates.TemplateResponse(
        "player_offers.html",
        {
            "request": request,
            "user": user,
            "offers": offers,
            "wallet": wallet_summary,
            "supported_rooms": sorted(SUPPORTED_ROOMS),
            "awaiting_result_count": awaiting_result_count,
            "has_result_ids": has_result_ids,
            "result_map": result_map,
            "pix_transactions": pix_transactions,
            "withdrawal_requests": withdrawal_requests,
            "requires_auth": True,
        },
    )


@router.post("/player/offers/{offer_id}/update")
def update_player_offer(
    offer_id: int,
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
    if user.tipo not in ("jogador", "admin"):
        return RedirectResponse(url="/", status_code=303)
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para editar ofertas.")
    normalized_room = normalize_supported_room(room)
    if not normalized_room:
        raise HTTPException(status_code=400, detail="Sala/plataforma não suportada pelo SharkScope.")

    try:
        buyin_value = Decimal(str(buyin))
        markup_value = Decimal(str(markup))
        total_pct_value = Decimal(str(total_pct))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Valores inválidos para edição da oferta.") from exc

    if buyin_value <= 0 or markup_value <= 0:
        raise HTTPException(status_code=400, detail="Buy-in e markup devem ser maiores que zero.")
    if total_pct_value <= 0 or total_pct_value > 100:
        raise HTTPException(status_code=400, detail="Total disponível deve ser entre 0 e 100.")

    data_hora = None
    if start_time:
        try:
            local_dt = datetime.fromisoformat(start_time)
            if local_dt.tzinfo is None:
                local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
            data_hora = local_dt.replace(tzinfo=None)
        except ValueError:
            data_hora = None

    # FOR UPDATE não pode ser usado com LEFT JOIN (joinedload) no PostgreSQL; buscar oferta sem join
    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id, StakeOffer.player_id == user.id)
        .with_for_update(of=StakeOffer)
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    tournament = db.get(Tournament, offer.tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Torneio da oferta não encontrado.")
    if tournament.status != "Aberto":
        raise HTTPException(status_code=400, detail="Somente torneios em aberto podem ser editados.")

    has_investments = db.execute(select(Investment.id).where(Investment.offer_id == offer.id)).scalars().first()
    if has_investments:
        raise HTTPException(
            status_code=400,
            detail="Não é possível editar a oferta pois já existem investidores vinculados.",
        )

    if _is_offer_closed_by_start_time(offer):
        raise HTTPException(status_code=400, detail="Oferta encerrada pelo horário, não pode mais ser editada.")

    tournament.nome = tournament_name.strip()
    tournament.sharkscope_id = normalized_room
    tournament.plataforma = normalized_room
    tournament.buyin = buyin_value
    tournament.data_hora = data_hora

    offer.markup = markup_value
    offer.total_disponivel_pct = total_pct_value
    offer.vendido_pct = Decimal("0")

    total_required = q_money(((buyin_value * markup_value) * total_pct_value) / Decimal("100"))
    escrow = db.execute(select(TournamentEscrow).where(TournamentEscrow.offer_id == offer.id).with_for_update()).scalars().first()
    if escrow:
        escrow.total_required = total_required
        escrow.deadline_at = data_hora
        if not has_investments:
            escrow.total_collected = Decimal("0")
            escrow.status = "COLLECTING"
    sync_offer_escrow(db, offer)
    db.commit()

    return RedirectResponse(url="/player/offers", status_code=303)


@router.post("/player/offers/{offer_id}/confirm-play")
def confirm_player_will_play(
    offer_id: int,
    request: Request,
    force_partial: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo not in ("jogador", "admin"):
        return RedirectResponse(url="/", status_code=303)
    ensure_user_not_blocked(user)
    iniciar_sem_meta = force_partial.strip().lower() in ("1", "true", "yes", "on")

    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id, StakeOffer.player_id == user.id)
        .with_for_update(of=StakeOffer)
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    sync_offer_escrow(db, offer)
    if iniciar_sem_meta:
        result = force_complete_and_release_escrow(db, offer)
    else:
        result = release_offer_escrow_to_player(db, offer)
    if Decimal(str(result["released_total"])) <= 0:
        # Para submit vindo de formulário HTML, evita tela branca de erro JSON.
        accept_header = (request.headers.get("accept") or "").lower()
        wants_json = "application/json" in accept_header and "text/html" not in accept_header
        if wants_json:
            raise HTTPException(
                status_code=400,
                detail="Não há saldo em escrow liberável. Se quiser iniciar mesmo sem atingir a meta, use \"Iniciar partida (mesmo sem 100%)\".",
            )
        return RedirectResponse(url="/player/offers?tab=ofertas&play_error=no_escrow", status_code=303)
    db.commit()
    return RedirectResponse(url="/player/offers?tab=ofertas&playing=1", status_code=303)


@router.post("/player/offers/{offer_id}/decline-play")
def decline_player_will_play(
    offer_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.tipo not in ("jogador", "admin"):
        return RedirectResponse(url="/", status_code=303)
    ensure_user_not_blocked(user)

    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id, StakeOffer.player_id == user.id)
        .with_for_update(of=StakeOffer)
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    tournament = db.get(Tournament, offer.tournament_id)
    if tournament and tournament.status == "Jogando":
        raise HTTPException(status_code=400, detail="Partida já iniciada, não é possível cancelar.")
    sync_offer_escrow(db, offer)
    refund_offer_escrow(db, offer, reason="PLAYER_DECLINED")
    db.commit()
    return RedirectResponse(url="/player/offers?declined=1", status_code=303)


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
    # Mesmo critério do GET /player/offers: admin tem perfil jogador implícito
    if user.tipo not in ("jogador", "admin"):
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
            local_dt = datetime.fromisoformat(start_time)
            if local_dt.tzinfo is None:
                local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
            data_hora = local_dt.replace(tzinfo=None)
        except ValueError:
            data_hora = None

    now_sp = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    deadline_at = data_hora or (now_sp + timedelta(hours=24))
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
    db.commit()
    return RedirectResponse(url="/player/offers?tab=ofertas", status_code=303)


@router.post("/api/invest")
async def invest(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    offer_id = payload.get("offer_id")
    amount_raw = payload.get("amount")
    if offer_id is None or amount_raw is None:
        raise HTTPException(status_code=400, detail="Dados inválidos. Verifique o valor e tente novamente.")

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor inválido. Informe um número válido com até 2 casas decimais.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="O valor do apoio deve ser maior que zero.")

    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para continuar com o apoio.")
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para investir.")
    if not user.wallet:
        raise HTTPException(status_code=400, detail="Carteira não encontrada. Atualize a página e tente novamente.")

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
        raise HTTPException(status_code=400, detail="Esta oferta não está mais aberta para novos apoios.")
    if _is_offer_closed_by_start_time(offer):
        raise HTTPException(status_code=400, detail="Esta oferta já foi encerrada para apoio.")

    buyin = Decimal(str(tournament.buyin))
    markup = Decimal(str(offer.markup))
    total_pct = Decimal(str(offer.total_disponivel_pct))
    sum_sold = db.execute(
        select(func.coalesce(func.sum(Investment.pct_comprada), 0)).where(Investment.offer_id == offer.id)
    ).scalar_one()
    sold_pct = q_pct(Decimal(str(sum_sold or 0)))
    available_pct = total_pct - sold_pct

    if buyin <= 0 or markup <= 0:
        raise HTTPException(status_code=400, detail="Buy-in ou markup inválido nesta oferta. Tente novamente mais tarde.")
    share_pct = (amount / (buyin * markup)) * Decimal("100")
    share_pct = q_pct(share_pct)
    if share_pct <= 0:
        raise HTTPException(status_code=400, detail="Valor do apoio resulta em percentual inválido.")
    if share_pct > available_pct + Decimal("0.01"):
        raise HTTPException(
            status_code=400,
            detail=f"Não há cota suficiente para esse valor (disponível: {float(available_pct):.1f}%). Recarregue a página e tente novamente.",
        )

    backer_wallet = ensure_wallet_for_user(db, user.id, with_lock=True)
    if backer_wallet.saldo_disponivel < amount:
        raise HTTPException(status_code=400, detail="Saldo insuficiente para concluir este apoio.")

    backer_wallet.saldo_disponivel = backer_wallet.saldo_disponivel - amount
    backer_wallet.saldo_em_jogo = backer_wallet.saldo_em_jogo + amount
    player_wallet = ensure_wallet_for_user(db, offer.player_id, with_lock=True)
    player_wallet.saldo_em_jogo = Decimal(str(player_wallet.saldo_em_jogo or 0)) + amount
    offer.vendido_pct = sold_pct + share_pct
    investment = Investment(
        offer_id=offer.id,
        backer_id=user.id,
        valor_investido=amount,
        pct_comprada=share_pct,
        lucro_recebido=Decimal("0"),
    )
    db.add(investment)
    db.flush()  # Adicionado para forçar que o registro vá para o banco e sync_offer_escrow somme 100% da cotação
    sync_offer_escrow(db, offer)
    db.commit()
    return {"success": True}


@router.post("/api/invest/update")
async def invest_update(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    investment_id = payload.get("investment_id")
    new_amount_raw = payload.get("amount")

    if investment_id is None or new_amount_raw is None:
        raise HTTPException(status_code=400, detail="Dados inválidos.")

    try:
        new_amount = Decimal(str(new_amount_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor inválido.")
    if new_amount <= 0:
        raise HTTPException(status_code=400, detail="O valor deve ser maior que zero.")

    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login.")
    ensure_user_not_blocked(user)

    inv_stmt = (
        select(Investment)
        .where(Investment.id == investment_id, Investment.backer_id == user.id)
        .options(joinedload(Investment.offer).joinedload(StakeOffer.tournament))
        .with_for_update(of=Investment)
    )
    investment = db.execute(inv_stmt).scalars().first()
    if not investment:
        raise HTTPException(status_code=404, detail="Apoio não encontrado.")

    offer = investment.offer
    tournament = offer.tournament
    
    can_edit = (
        tournament.status == "Aberto" and 
        not _is_offer_closed_by_start_time(offer) and 
        offer.escrow_status in ("COLLECTING", "COMPLETE")
    )
    
    if not can_edit:
        raise HTTPException(status_code=400, detail="Esta oferta não permite mais edições (partida já iniciada ou meta já confirmada).")

    # Recalcula percentual
    buyin = Decimal(str(tournament.buyin))
    markup = Decimal(str(offer.markup))
    if buyin <= 0 or markup <= 0:
        raise HTTPException(status_code=400, detail="Dados da oferta inválidos.")

    new_share_pct = q_pct((new_amount / (buyin * markup)) * Decimal("100"))
    
    # Verifica disponibilidade (meta total)
    sum_sold = db.execute(
        select(func.coalesce(func.sum(Investment.pct_comprada), 0))
        .where(Investment.offer_id == offer.id, Investment.id != investment.id)
    ).scalar_one()
    others_sold_pct = q_pct(Decimal(str(sum_sold or 0)))
    total_pct = Decimal(str(offer.total_disponivel_pct))
    
    if others_sold_pct + new_share_pct > total_pct + Decimal("0.001"):
        avail_pct = max(Decimal("0"), total_pct - others_sold_pct)
        # Calcula quanto em dólar isso representa
        max_usd = q_money((avail_pct / Decimal("100")) * (buyin * markup))
        raise HTTPException(
            status_code=400,
            detail=f"Não há cota suficiente. Máximo permitido: US$ {float(max_usd):.2f} ({float(avail_pct):.1f}%).",
        )

    # Ajuste de carteira
    diff = new_amount - investment.valor_investido
    wallet = ensure_wallet_for_user(db, user.id, with_lock=True)
    if diff > 0 and wallet.saldo_disponivel < diff:
        raise HTTPException(status_code=400, detail="Saldo insuficiente para aumentar o apoio.")

    wallet.saldo_disponivel -= diff
    wallet.saldo_em_jogo += diff
    
    player_wallet = ensure_wallet_for_user(db, offer.player_id, with_lock=True)
    player_wallet.saldo_em_jogo += diff

    # Atualiza investimento e oferta
    investment.valor_investido = new_amount
    investment.pct_comprada = new_share_pct

    # Sincroniza vendido_pct na oferta
    total_sold_pct = db.execute(
        select(func.coalesce(func.sum(Investment.pct_comprada), 0))
        .where(Investment.offer_id == offer.id)
    ).scalar_one()
    offer.vendido_pct = q_pct(Decimal(str(total_sold_pct or 0)))

    # Sincroniza escrow
    from routers.escrow import sync_offer_escrow
    sync_offer_escrow(db, offer)

    db.commit()
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
        raise HTTPException(status_code=400, detail="O valor da proposta deve ser maior que zero.")
    if proposed_markup < MIN_PROPOSED_MARKUP:
        raise HTTPException(status_code=400, detail=f"Markup proposto deve ser >= {MIN_PROPOSED_MARKUP}.")

    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para enviar sua proposta.")
    if user.tipo != "apoiador":
        raise HTTPException(status_code=403, detail="Apenas apoiadores podem enviar bids.")
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para enviar propostas.")

    wallet_stmt = select(Wallet).where(Wallet.user_id == user.id).with_for_update()
    wallet = db.execute(wallet_stmt).scalars().first()
    if not wallet:
        raise HTTPException(status_code=400, detail="Carteira não encontrada. Atualize a página e tente novamente.")

    offer_stmt = (
        select(StakeOffer)
        .where(StakeOffer.id == offer_id)
        .options(joinedload(StakeOffer.tournament))
    )
    offer = db.execute(offer_stmt).scalars().first()
    if not offer or not offer.tournament:
        raise HTTPException(status_code=404, detail="Oferta não encontrada.")
    if offer.escrow_status != "COLLECTING":
        raise HTTPException(status_code=400, detail="Esta oferta não está mais aberta para propostas.")
    if _is_offer_closed_by_start_time(offer):
        raise HTTPException(status_code=400, detail="Esta oferta já foi encerrada para novas propostas.")
    buyin = Decimal(str(offer.tournament.buyin))
    if buyin <= 0:
        raise HTTPException(status_code=400, detail="Buy-in inválido para receber propostas.")
    total_pct = Decimal(str(offer.total_disponivel_pct))
    sold_pct = Decimal(str(offer.vendido_pct))
    available_pct = total_pct - sold_pct
    share_pct = (amount / (buyin * proposed_markup)) * Decimal("100")
    if share_pct > available_pct:
        raise HTTPException(status_code=400, detail="Percentual disponível insuficiente para essa proposta.")

    saldo_disp = Decimal(str(wallet.saldo_disponivel))
    if saldo_disp < amount:
        raise HTTPException(status_code=400, detail="Saldo disponível insuficiente para enviar esta proposta.")

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
    db.commit()
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
        raise HTTPException(status_code=401, detail="Faça login para responder a proposta.")
    if user.tipo not in ("jogador", "admin"):
        raise HTTPException(status_code=403, detail="Apenas o dono da oferta pode responder.")
    ensure_user_not_blocked(user)
    if not is_user_kyc_approved(user, db):
        raise HTTPException(status_code=403, detail="KYC pendente. Aguarde aprovação do admin para responder propostas.")

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
        raise HTTPException(status_code=400, detail="Esta proposta já foi respondida anteriormente.")

    offer = bid.offer
    if offer.player_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o dono da oferta pode responder.")
    if offer.escrow_status != "COLLECTING":
        raise HTTPException(status_code=400, detail="A oferta não está mais aberta para resposta de propostas.")

    backer_wallet_stmt = select(Wallet).where(Wallet.user_id == bid.backer_id).with_for_update()
    backer_wallet = db.execute(backer_wallet_stmt).scalars().first()
    if not backer_wallet:
        raise HTTPException(status_code=400, detail="Carteira do apoiador não encontrada para processar a resposta.")

    amount = Decimal(str(bid.amount))
    saldo_bloq = Decimal(str(getattr(backer_wallet, "saldo_bloqueado", 0) or 0))
    if saldo_bloq < amount:
        bid.status = "REJECTED"
        db.commit()
        raise HTTPException(status_code=400, detail="Saldo bloqueado insuficiente para processar esta proposta.")

    if action == "ACCEPT" and _is_offer_closed_by_start_time(offer):
        backer_wallet.saldo_bloqueado = saldo_bloq - amount
        backer_wallet.saldo_disponivel = Decimal(str(backer_wallet.saldo_disponivel)) + amount
        bid.status = "REJECTED"
        db.commit()
        raise HTTPException(status_code=400, detail="Esta oferta está encerrada e não aceita novas confirmações.")

    if action == "REJECT":
        backer_wallet.saldo_bloqueado = saldo_bloq - amount
        backer_wallet.saldo_disponivel = Decimal(str(backer_wallet.saldo_disponivel)) + amount
        bid.status = "REJECTED"
        db.commit()
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
        db.commit()
        raise HTTPException(
            status_code=400,
            detail="Percentual disponível na oferta não é mais suficiente para este valor/markup.",
        )

    backer_wallet.saldo_bloqueado = saldo_bloq - amount
    backer_wallet.saldo_em_jogo = Decimal(str(backer_wallet.saldo_em_jogo)) + amount
    player_wallet = ensure_wallet_for_user(db, offer.player_id, with_lock=True)
    player_wallet.saldo_em_jogo = Decimal(str(player_wallet.saldo_em_jogo or 0)) + amount
    offer.vendido_pct = sold_pct + share_pct
    investment = Investment(
        offer_id=offer.id,
        backer_id=bid.backer_id,
        valor_investido=amount,
        pct_comprada=share_pct,
        lucro_recebido=Decimal("0"),
    )
    db.add(investment)
    db.flush()
    bid.status = "ACCEPTED"
    sync_offer_escrow(db, offer)
    db.commit()
    return {"success": True, "status": "ACCEPTED"}
