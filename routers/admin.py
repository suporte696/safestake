from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, or_, select, func
from sqlalchemy.orm import Session, joinedload
from urllib.parse import quote_plus

from constants import SUPPORTED_ROOMS, normalize_supported_room
from db import get_db
from models import (
    CallSchedule,
    Investment,
    MatchResult,
    PrizeDistribution,
    StakeOffer,
    Tournament,
    TournamentEscrow,
    UserDocument,
    Wallet,
    WithdrawalRequest,
)
from routers.auth import ensure_admin_user, fetch_current_user, get_password_hash, get_wallet_summary, is_strong_password, verify_password
from services.notifications import create_notification
from services.ptax import get_usd_brl_ptax_rate

templates = Jinja2Templates(directory="templates")
router = APIRouter()
FEE_RATE = Decimal("0.08")
MONEY_Q = Decimal("0.01")
PCT_Q = Decimal("0.0001")
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def q_pct(value: Decimal) -> Decimal:
    """Arredonda percentuais para 4 casas decimais."""
    return Decimal(value).quantize(PCT_Q, rounding=ROUND_HALF_UP)


def get_wallet_for_update(db: Session, user_id: int) -> Wallet:
    """
    Garante que a carteira exista e retorna já bloqueada para escrita.
    Se não existir, cria uma carteira zerada para o usuário.
    """
    wallet = db.execute(
        select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    ).scalars().first()
    if not wallet:
        wallet = Wallet(
            user_id=user_id,
            saldo_disponivel=Decimal("0"),
            saldo_bloqueado=Decimal("0"),
            saldo_em_jogo=Decimal("0"),
        )
        db.add(wallet)
        db.flush()
        wallet = db.execute(
            select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        ).scalars().first()
    return wallet


@router.get("/admin/dev-check-balances")
def check_balances_dev(db: Session = Depends(get_db)):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    hoje = datetime.now(timezone.utc) - timedelta(hours=48)
    
    stmt = (
        select(Investment, User.nome, Wallet)
        .join(User, Investment.backer_id == User.id)
        .join(Wallet, Wallet.user_id == User.id)
        .where(Investment.created_at >= hoje)
    )
    results = db.execute(stmt).all()
    
    user_investments = {}
    for inv, nome, wallet in results:
        uid = inv.backer_id
        if uid not in user_investments:
            user_investments[uid] = {
                'nome': nome,
                'wallet_em_jogo': float(wallet.saldo_em_jogo),
                'wallet_disponivel': float(wallet.saldo_disponivel),
                'soma_investida_24h': 0.0
            }
        user_investments[uid]['soma_investida_24h'] += float(inv.valor_investido)
        
    inconsistencias = []
    for uid, data in user_investments.items():
        if data['wallet_em_jogo'] < data['soma_investida_24h'] - 0.05:
            diff = data['soma_investida_24h'] - data['wallet_em_jogo']
            inconsistencias.append({
                "user_id": uid,
                "nome": data['nome'],
                "saldo_disponivel": data['wallet_disponivel'],
                "saldo_em_jogo": data['wallet_em_jogo'],
                "soma_investimentos_ativos": data['soma_investida_24h'],
                "diferenca_nao_descontada": diff
            })
            
    return {"inconsistencias": inconsistencias}

@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
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
        .options(joinedload(Tournament.offers).joinedload(StakeOffer.player))
        .order_by(Tournament.data_hora.asc().nulls_last(), Tournament.id.desc())
    )
    active_tournaments = db.execute(active_tournaments_stmt).unique().scalars().all()
    active_ids = [item.id for item in active_tournaments]

    can_close_tournament: dict[int, bool] = {}
    for t in active_tournaments:
        meta_atingida = any(getattr(o, "escrow_status", None) == "COMPLETE" for o in (t.offers or []))
        jogador_confirmou = t.status == "Jogando"
        can_close_tournament[t.id] = meta_atingida or jogador_confirmou

    supporters_by_tournament: dict[int, list[dict]] = {}
    if active_ids:
        active_investment_stmt = (
            select(Investment)
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(StakeOffer.tournament_id.in_(active_ids))
            .options(joinedload(Investment.backer), joinedload(Investment.offer))
            .order_by(StakeOffer.tournament_id.asc(), Investment.id.asc())
        )
        for inv in db.execute(active_investment_stmt).scalars().all():
            if not inv.offer:
                continue
            supporters_by_tournament.setdefault(inv.offer.tournament_id, []).append(
                {
                    "id": inv.id,
                    "offer_id": inv.offer_id,
                    "name": inv.backer.nome if inv.backer else f"Usuário #{inv.backer_id}",
                    "pct": Decimal(str(inv.pct_comprada or 0)),
                    "amount": Decimal(str(inv.valor_investido or 0)),
                    "markup": float(inv.offer.markup) if inv.offer else 1.0,
                    "total_disponivel_pct": float(inv.offer.total_disponivel_pct) if inv.offer else 100,
                    "vendido_pct": float(inv.offer.vendido_pct) if inv.offer else 0,
                }
            )

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
            valor_inv = Decimal(str(inv.valor_investido or 0))
            lucro = Decimal(str(inv.lucro_recebido or 0))
            rows.append(
                {
                    "investment_id": inv.id,
                    "apoiador_nome": inv.backer.nome if inv.backer else f"Usuário #{inv.backer_id}",
                    "valor_investido": valor_inv,
                    "valor_receber": valor_inv + lucro,
                    "pix_key": (inv.backer.pix_key if inv.backer and inv.backer.pix_key else "-"),
                    "payout_status": inv.payout_status,
                }
            )
        settlement_rows_by_tournament[tournament.id] = rows

    password_error = request.query_params.get("pwd_error")
    password_success = request.query_params.get("pwd_success")
    profile_success = request.query_params.get("profile_success")
    withdrawal_requests = db.execute(
        select(WithdrawalRequest)
        .options(joinedload(WithdrawalRequest.user), joinedload(WithdrawalRequest.reviewed_by_user))
        .order_by(WithdrawalRequest.created_at.desc(), WithdrawalRequest.id.desc())
        .limit(40)
    ).scalars().all()

    try:
        ptax_rate = await get_usd_brl_ptax_rate()
    except Exception:
        ptax_rate = Decimal("5.00")
    ptax_rate_float = float(ptax_rate)

    def _mask_pix_display(key: str | None) -> str:
        if not key or not key.strip():
            return ""
        s = key.strip()
        digits = "".join(c for c in s if c.isdigit())
        if len(digits) == 11 and len(s) >= 11:
            return "***.***.***-" + s[-2:]
        if "@" in s:
            parts = s.split("@", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0][:1] + "***@" + "***." + (parts[1].split(".")[-1] if "." in parts[1] else "***")
            return s[:3] + "***" if len(s) > 3 else "***"
        if len(digits) >= 10:
            return "(**) *****-" + s[-4:]
        return s[:4] + "***" if len(s) > 4 else "***"

    pix_masked_by_wr = {wr.id: _mask_pix_display(wr.pix_key) for wr in withdrawal_requests}

    user_profile_photo_url = getattr(user, "profile_photo_url", None) if user else None

    # --- Lógica migrada da aba Resultados ---
    pending_stmt = (
        select(MatchResult)
        .where(MatchResult.review_status.in_(["PENDING", "UNDER_REVIEW"]))
        .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
        .order_by(MatchResult.submitted_at.asc(), MatchResult.id.asc())
    )
    pending_results_db = db.execute(pending_stmt).scalars().all()

    reviewed_stmt = (
        select(MatchResult)
        .where(MatchResult.review_status.in_(("APPROVED", "REJECTED")))
        .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
        .order_by(MatchResult.reviewed_at.desc(), MatchResult.id.desc())
        .limit(40)
    )
    reviewed_results_db = db.execute(reviewed_stmt).scalars().all()

    distribution_stmt = (
        select(PrizeDistribution)
        .where(PrizeDistribution.match_result_id.in_([item.id for item in reviewed_results_db] or [-1]))
    )
    distributions = db.execute(distribution_stmt).scalars().all()
    distribution_by_result_db: dict[int, list[PrizeDistribution]] = {}
    for item in distributions:
        distribution_by_result_db.setdefault(item.match_result_id, []).append(item)

    result_ids_by_tournament_player: dict[tuple[int, int], int] = {}
    for r in pending_results_db + reviewed_results_db:
        result_ids_by_tournament_player[(r.tournament_id, r.player_id)] = r.id

    supporters_by_result_db: dict[int, list[dict]] = {}
    if result_ids_by_tournament_player:
        offer_conditions = [
            and_(StakeOffer.tournament_id == t, StakeOffer.player_id == p)
            for (t, p) in result_ids_by_tournament_player.keys()
        ]
        offer_stmt = select(StakeOffer).where(or_(*offer_conditions)).options(joinedload(StakeOffer.tournament))
        offer_rows = db.execute(offer_stmt).scalars().all()
        offer_id_to_result_id: dict[int, int] = {}
        for offer in offer_rows:
            if offer.tournament_id is not None and offer.player_id is not None:
                key = (offer.tournament_id, offer.player_id)
                if key in result_ids_by_tournament_player:
                    offer_id_to_result_id[offer.id] = result_ids_by_tournament_player[key]
        offer_ids = list(offer_id_to_result_id.keys())
        if offer_ids:
            inv_stmt = (
                select(Investment)
                .where(Investment.offer_id.in_(offer_ids))
                .options(joinedload(Investment.backer), joinedload(Investment.offer))
            )
            for inv in db.execute(inv_stmt).scalars().all():
                result_id = offer_id_to_result_id.get(inv.offer_id) if inv.offer_id else None
                if result_id is not None:
                    supporters_by_result_db.setdefault(result_id, []).append(
                        {
                            "name": inv.backer.nome if inv.backer else f"Usuário #{inv.backer_id}",
                            "amount": inv.valor_investido,
                            "pct": inv.pct_comprada,
                        }
                    )

    # Prévia da divisão para resultados em revisão
    preview_by_result_db: dict[int, list[dict]] = {}
    for pending in pending_results_db:
        supporters = supporters_by_result_db.get(pending.id, [])
        if not supporters:
            continue
        try:
            total_sent = q_money(Decimal(str(pending.valor_enviado)))
        except (InvalidOperation, TypeError):
            continue
        if total_sent <= 0:
            continue

        items: list[dict] = []
        backer_gross_total = Decimal("0")
        fee_total = Decimal("0")

        for s in supporters:
            try:
                pct = Decimal(str(s.get("pct") or 0))
                invested_amount = q_money(Decimal(str(s.get("amount") or 0)))
            except InvalidOperation:
                continue
            gross_amount = q_money(total_sent * pct / Decimal("100"))
            gain_amount = gross_amount - invested_amount
            if gain_amount < 0:
                gain_amount = Decimal("0")
            platform_fee = q_money(gain_amount * FEE_RATE)
            net_amount = gross_amount - platform_fee

            backer_gross_total += gross_amount
            fee_total += platform_fee

            items.append(
                {
                    "role": "Apoiador",
                    "name": s.get("name") or "-",
                    "net_amount": net_amount,
                    "platform_fee": platform_fee,
                }
            )

        backer_gross_total = q_money(backer_gross_total)
        fee_total = q_money(fee_total)
        player_amount = q_money(total_sent - backer_gross_total)

        items.append(
            {
                "role": "Jogador",
                "name": pending.player.nome if pending.player else "Jogador",
                "net_amount": player_amount,
                "platform_fee": Decimal("0"),
            }
        )
        if fee_total > 0:
            items.append(
                {
                    "role": "Plataforma",
                    "name": "SAFE STAKE",
                    "net_amount": fee_total,
                    "platform_fee": fee_total,
                }
            )
        preview_by_result_db[pending.id] = items
    # --- Fim Lógica migrada da aba Resultados ---

    # --- Lucros da Plataforma ---
    total_platform_fee = db.execute(
        select(func.sum(PrizeDistribution.platform_fee))
        .where(PrizeDistribution.platform_fee > 0)
    ).scalar() or Decimal("0")

    recent_platform_fees = db.execute(
        select(PrizeDistribution)
        .options(joinedload(PrizeDistribution.match_result).joinedload(MatchResult.tournament))
        .where(PrizeDistribution.platform_fee > 0)
        .order_by(PrizeDistribution.created_at.desc())
        .limit(50)
    ).scalars().all()
    # --- Fim Lucros da Plataforma ---

    pending_results_data = {}
    for r in pending_results_db:
        r_supporters = [
            {
                "name": s["name"],
                "pct": f"{s['pct']:.2f}",
                "amount": f"{s['amount']:.2f}"
            }
            for s in supporters_by_result_db.get(r.id, [])
        ]
        r_preview = [
            {
                "role": item["role"],
                "name": item["name"],
                "net_amount": f"{item['net_amount']:.2f}",
                "platform_fee": str(item.get("platform_fee", "0"))
            }
            for item in preview_by_result_db.get(r.id, [])
        ]
        pending_results_data[str(r.id)] = {
            "id": r.id,
            "tournament_id": r.tournament.id,
            "tournament_name": r.tournament.nome,
            "plataforma": r.tournament.plataforma or "Sala",
            "buyin": f"{r.tournament.buyin:.2f}",
            "player_name": r.player.nome,
            "posicao_final": r.posicao_final,
            "submitted_at": r.submitted_at.strftime("%d/%m/%Y %H:%M") if r.submitted_at else "-",
            "valor_premio": f"{r.valor_premio:.2f}",
            "valor_enviado": f"{r.valor_enviado:.2f}",
            "print_url": r.print_url or "#",
            "comprovante_url": r.comprovante_url or "#",
            "supporters": r_supporters,
            "preview": r_preview
        }

    response = templates.TemplateResponse(
        "admin_dashboard.html",
        {
            "request": request,
            "user": user,
            "user_profile_photo_url": user_profile_photo_url,
            "wallet": get_wallet_summary(user),
            "pending_docs": pending_docs,
            "reviewed_docs": reviewed_docs,
            "active_tournaments": active_tournaments,
            "supporters_by_tournament": supporters_by_tournament,
            "finalized_tournaments": finalized_tournaments,
            "settlement_rows_by_tournament": settlement_rows_by_tournament,
            "withdrawal_requests": withdrawal_requests,
            "password_error": password_error,
            "password_success": password_success,
            "profile_success": profile_success,
            "supported_rooms": sorted(SUPPORTED_ROOMS),
            "ptax_rate": ptax_rate_float,
            "pix_masked_by_wr": pix_masked_by_wr,
            "can_close_tournament": can_close_tournament,
            "finalized_tournament_ids": [t.id for t in finalized_tournaments],
            "requires_auth": True,
            "tournament_created": request.query_params.get("created") == "1",
            "pending_results": pending_results_db,
            "reviewed_results": reviewed_results_db,
            "distribution_by_result": distribution_by_result_db,
            "supporters_by_result": supporters_by_result_db,
            "preview_by_result": preview_by_result_db,
            "pending_results_data": pending_results_data,
            "total_platform_fee": total_platform_fee,
            "recent_platform_fees": recent_platform_fees,
        },
    )
    # Evita cache antigo: cliente sempre recebe HTML atual (evita bug de Enter/Publicar enviando form errado)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@router.post("/admin/withdrawals/{withdrawal_id}/approve")
def approve_withdrawal(
    withdrawal_id: int,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    wr = db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id)
    ).scalars().first()
    if not wr:
        raise HTTPException(status_code=404, detail="Solicitação de saque não encontrada.")
    if wr.status != "PENDING":
        raise HTTPException(status_code=400, detail="Esta solicitação já foi revisada anteriormente.")

    wallet = get_wallet_for_update(db, wr.user_id)
    amount = q_money(Decimal(str(wr.amount or 0)))
    saldo_disp = q_money(Decimal(str(wallet.saldo_disponivel or 0)))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor de saque inválido na solicitação.")
    if saldo_disp < amount:
        raise HTTPException(status_code=400, detail="O usuário não possui saldo suficiente para aprovar este saque.")

    wallet.saldo_disponivel = q_money(saldo_disp - amount)
    wr.status = "APPROVED"
    wr.admin_note = note.strip()[:255] if note else None
    wr.reviewed_by = user.id
    wr.reviewed_at = datetime.now(timezone.utc)

    create_notification(
        db,
        user_id=wr.user_id,
        n_type="WITHDRAWAL_APPROVED",
        title="Saque aprovado",
        message=f"Sua solicitação de saque de US$ {amount:.2f} foi aprovada.",
        action_url="/player/offers?tab=historico",
        target_role="jogador",
    )
    db.commit()

    return RedirectResponse(url="/admin/dashboard?tab=saques&withdraw_success=1", status_code=303)


@router.post("/admin/withdrawals/{withdrawal_id}/reject")
def reject_withdrawal(
    withdrawal_id: int,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    wr = db.execute(
        select(WithdrawalRequest).where(WithdrawalRequest.id == withdrawal_id)
    ).scalars().first()
    if not wr:
        raise HTTPException(status_code=404, detail="Solicitação de saque não encontrada.")
    if wr.status != "PENDING":
        raise HTTPException(status_code=400, detail="Esta solicitação já foi revisada anteriormente.")

    wr.status = "REJECTED"
    wr.admin_note = note.strip()[:255] if note else None
    wr.reviewed_by = user.id
    wr.reviewed_at = datetime.now(timezone.utc)

    create_notification(
        db,
        user_id=wr.user_id,
        n_type="WITHDRAWAL_REJECTED",
        title="Saque rejeitado",
        message=(
            "Sua solicitação de saque foi rejeitada."
            + (f" Motivo: {wr.admin_note}" if wr.admin_note else "")
        ),
        action_url="/player/offers?tab=historico",
        target_role="jogador",
    )
    db.commit()

    return RedirectResponse(url="/admin/dashboard?tab=saques&withdraw_success=1", status_code=303)


@router.post("/admin/profile/update")
def update_admin_profile(
    request: Request,
    nome: str = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    nome = (nome or "").strip()
    if not nome:
        return RedirectResponse(url="/admin/dashboard?pwd_error=" + quote_plus("Nome não pode ser vazio."), status_code=303)
    user.nome = nome[:120]
    db.commit()
    return RedirectResponse(url="/admin/dashboard?profile_success=1", status_code=303)


@router.post("/admin/profile/photo")
async def update_admin_profile_photo(
    request: Request,
    photo: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    from services.storage import save_profile_photo

    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    if not photo or not getattr(photo, "filename", None) or not photo.filename.strip():
        return RedirectResponse(url="/admin/dashboard?pwd_error=" + quote_plus("Selecione uma imagem."), status_code=303)
    try:
        url = await save_profile_photo(photo, user_id=user.id)
    except ValueError as e:
        return RedirectResponse(url="/admin/dashboard?pwd_error=" + quote_plus(str(e)), status_code=303)
    user.profile_photo_url = url
    db.commit()
    return RedirectResponse(url="/admin/dashboard?profile_success=1", status_code=303)


@router.post("/admin/password/update")
def update_admin_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    if not verify_password(current_password, user.password_hash):
        msg = quote_plus("Senha atual inválida.")
        return RedirectResponse(url=f"/admin/dashboard?pwd_error={msg}", status_code=303)
    if new_password != confirm_password:
        msg = quote_plus("A confirmação da nova senha não confere.")
        return RedirectResponse(url=f"/admin/dashboard?pwd_error={msg}", status_code=303)
    if not is_strong_password(new_password):
        msg = quote_plus("A nova senha deve ter 8+ caracteres, letras e números.")
        return RedirectResponse(url=f"/admin/dashboard?pwd_error={msg}", status_code=303)
    if verify_password(new_password, user.password_hash):
        msg = quote_plus("A nova senha não pode ser igual à atual.")
        return RedirectResponse(url=f"/admin/dashboard?pwd_error={msg}", status_code=303)

    user.password_hash = get_password_hash(new_password)
    db.commit()
    return RedirectResponse(url="/admin/dashboard?pwd_success=1", status_code=303)




@router.post("/api/tournaments/{tournament_id}/close")
async def close_tournament(tournament_id: int, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para continuar.")
    ensure_admin_user(user)

    payload = await request.json()
    prize_raw = payload.get("prize_amount")
    if prize_raw is None:
        raise HTTPException(status_code=400, detail="Informe o valor do prêmio para encerrar o torneio.")

    try:
        prize_amount = q_money(Decimal(str(prize_raw)))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor de prêmio inválido. Use apenas números.")
    if prize_amount < 0:
        raise HTTPException(status_code=400, detail="Valor de prêmio não pode ser negativo.")

    tournament_stmt = (
        select(Tournament)
        .where(Tournament.id == tournament_id)
        .options(joinedload(Tournament.offers))
        .with_for_update(of=Tournament)
    )
    tournament = db.execute(tournament_stmt).unique().scalars().first()
    if not tournament:
        raise HTTPException(status_code=404, detail="Torneio não encontrado.")
    meta_atingida = any(getattr(o, "escrow_status", None) == "COMPLETE" for o in (tournament.offers or []))
    jogador_confirmou = tournament.status == "Jogando"
    if not (meta_atingida or jogador_confirmou):
        raise HTTPException(
            status_code=400,
            detail="Só é possível encerrar o torneio quando a meta total for atingida ou o jogador tiver confirmado que vai jogar.",
        )

    total_allocated = Decimal("0")
    updated_count = 0

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
        inv.payout_status = "PAID"
        total_allocated += valor_receber
        updated_count += 1

    tournament.status = "Finalizado"
    db.commit()

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

    stmt = (
        select(Investment)
        .where(Investment.id == investment_id)
        .options(joinedload(Investment.backer))
        .with_for_update(of=Investment)
    )
    investment = db.execute(stmt).scalars().first()
    if not investment:
        raise HTTPException(status_code=404, detail="Investimento não encontrado.")
    if investment.payout_status == "PAID":
        return {"success": True, "investment_id": investment_id, "payout_status": "PAID"}

    valor_investido = q_money(Decimal(str(investment.valor_investido or 0)))
    lucro = q_money(Decimal(str(investment.lucro_recebido or 0)))
    total_a_creditar = valor_investido + lucro

    backer_wallet = get_wallet_for_update(db, investment.backer_id)
    backer_wallet.saldo_disponivel = q_money(Decimal(str(backer_wallet.saldo_disponivel or 0)) + total_a_creditar)
    em_jogo = Decimal(str(backer_wallet.saldo_em_jogo or 0))
    backer_wallet.saldo_em_jogo = q_money(max(Decimal("0"), em_jogo - valor_investido))

    investment.payout_status = "PAID"
    db.commit()

    return {"success": True, "investment_id": investment_id, "payout_status": "PAID"}


@router.post("/admin/investments/{investment_id}/update-value")
def update_investment_value(
    investment_id: int,
    request: Request,
    valor_investido: float = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    inv = db.execute(select(Investment).where(Investment.id == investment_id)).scalars().first()
    if not inv:
        raise HTTPException(status_code=404, detail="Investimento não encontrado.")
    if inv.payout_status != "PENDING":
        raise HTTPException(status_code=400, detail="Só é possível editar o valor do apoio enquanto o pagamento estiver pendente.")
    try:
        val = q_money(Decimal(str(valor_investido)))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor inválido.")
    if val < 0:
        raise HTTPException(status_code=400, detail="O valor do apoio não pode ser negativo.")
    inv.valor_investido = val
    db.commit()
    return RedirectResponse(url="/admin/dashboard?updated_inv=1&tab=acerto", status_code=303)


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


@router.post("/admin/results/{result_id}/update-values")
def update_result_values(
    result_id: int,
    request: Request,
    valor_premio: float = Form(...),
    valor_enviado: float = Form(...),
    embed: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    result = db.execute(
        select(MatchResult).where(MatchResult.id == result_id)
    ).scalars().first()
    if not result:
        raise HTTPException(status_code=404, detail="Resultado não encontrado.")
    if result.review_status not in ("UNDER_REVIEW", "PENDING"):
        raise HTTPException(status_code=400, detail="Só é possível editar valores enquanto o resultado estiver pendente ou em revisão.")

    premio = q_money(Decimal(str(valor_premio)))
    enviado = q_money(Decimal(str(valor_enviado)))
    if premio < 0 or enviado < 0:
        raise HTTPException(status_code=400, detail="Valores devem ser não negativos.")

    result.valor_premio = premio
    result.valor_enviado = enviado
    db.commit()
    url = "/admin/dashboard?updated=1"
    return RedirectResponse(url=url, status_code=303)


@router.post("/admin/results/{result_id}/approve")
def approve_result(
    result_id: int,
    request: Request,
    embed: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)

    with db.begin_nested():
        result_stmt = (
            select(MatchResult)
            .where(MatchResult.id == result_id)
            .options(joinedload(MatchResult.player), joinedload(MatchResult.tournament))
            .with_for_update(of=MatchResult)
        )
        result = db.execute(result_stmt).scalars().first()
        if not result:
            raise HTTPException(status_code=404, detail="Resultado não encontrado.")
        if result.review_status not in ("UNDER_REVIEW", "PENDING"):
            raise HTTPException(status_code=400, detail="Este resultado já foi revisado anteriormente.")

        total_sent = q_money(Decimal(str(result.valor_enviado)))
        if total_sent < 0:
            raise HTTPException(status_code=400, detail="Valor enviado não pode ser negativo.")

        investment_stmt = (
            select(Investment)
            .join(StakeOffer, Investment.offer_id == StakeOffer.id)
            .where(StakeOffer.tournament_id == result.tournament_id)
            .options(joinedload(Investment.backer), joinedload(Investment.offer))
            .with_for_update(of=Investment)
        )
        investments = db.execute(investment_stmt).scalars().all()

        backer_gross_total = Decimal("0")
        fee_total = Decimal("0")
        total_invested = Decimal("0")
        for inv in investments:
            pct = Decimal(str(inv.pct_comprada or 0))
            invested_amount = q_money(Decimal(str(inv.valor_investido or 0)))
            gross_amount = q_money(total_sent * pct / Decimal("100"))
            gain_amount = gross_amount - invested_amount
            if gain_amount < 0:
                gain_amount = Decimal("0")
            platform_fee = q_money(gain_amount * FEE_RATE)
            net_amount = gross_amount - platform_fee

            inv.lucro_recebido = q_money(net_amount - invested_amount)
            inv.payout_status = "PAID"

            backer_wallet = get_wallet_for_update(db, inv.backer_id)
            backer_em_jogo = Decimal(str(backer_wallet.saldo_em_jogo or 0))
            backer_wallet.saldo_em_jogo = q_money(max(Decimal("0"), backer_em_jogo - invested_amount))
            backer_wallet.saldo_disponivel = q_money(Decimal(str(backer_wallet.saldo_disponivel or 0)) + net_amount)

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
            total_invested += invested_amount

        backer_gross_total = q_money(backer_gross_total)
        fee_total = q_money(fee_total)
        if backer_gross_total > total_sent:
            raise HTTPException(
                status_code=400,
                detail="Soma de percentuais dos apoiadores excede o valor enviado.",
            )

        player_amount = q_money(total_sent - backer_gross_total)
        player_wallet = get_wallet_for_update(db, result.player_id)
        player_em_jogo = Decimal(str(player_wallet.saldo_em_jogo or 0))
        player_wallet.saldo_em_jogo = q_money(max(Decimal("0"), player_em_jogo - q_money(total_invested)))
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
        create_notification(
            db,
            user_id=result.player_id,
            n_type="RESULT_APPROVED",
            title="Resultado aprovado ✓",
            message=(
                f"Seu resultado do torneio #{result.tournament_id} foi aprovado. "
                f"Distribuição financeira concluída."
            ),
            action_url="/player/offers?tab=torneios",
            target_role="jogador",
        )

    db.commit()
    url = "/admin/results"
    if embed == "1":
        url += "?embed=1"
    return RedirectResponse(url=url, status_code=303)


@router.post("/admin/results/{result_id}/reject")
def reject_result(
    result_id: int,
    request: Request,
    reason: str = Form(""),
    embed: str = Form(""),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    ensure_admin_user(user)
    stmt = select(MatchResult).where(MatchResult.id == result_id)
    result = db.execute(stmt).scalars().first()
    if result:
        if result.review_status not in ("UNDER_REVIEW", "PENDING"):
            raise HTTPException(status_code=400, detail="Este resultado já foi revisado anteriormente.")
        result.review_status = "REJECTED"
        result.admin_verified = False
        result.rejection_reason = reason.strip()[:255] if reason else None
        result.reviewed_by = user.id
        result.reviewed_at = datetime.now(timezone.utc)
        create_notification(
            db,
            user_id=result.player_id,
            n_type="RESULT_REJECTED",
            title="Resultado rejeitado ✗",
            message=(
                f"Seu resultado do torneio #{result.tournament_id} foi rejeitado."
                + (f" Motivo: {result.rejection_reason}" if result.rejection_reason else "")
            ),
            action_url=f"/player/results/new?tournament_id={result.tournament_id}",
            target_role="jogador",
        )
        db.commit()
    url = "/admin/dashboard"
    return RedirectResponse(url=url, status_code=303)


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
@router.post("/admin/invest/update")
async def admin_update_investment(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login.")
    ensure_admin_user(user)

    data = await request.json()
    investment_id = data.get("investment_id")
    new_amount = Decimal(str(data.get("amount") or 0))

    if not investment_id:
        raise HTTPException(status_code=400, detail="ID de investimento não informado.")
    if new_amount <= 0:
        raise HTTPException(status_code=400, detail="O valor deve ser maior que zero.")

    inv_stmt = (
        select(Investment)
        .where(Investment.id == investment_id)
        .options(joinedload(Investment.offer).joinedload(StakeOffer.tournament))
        .with_for_update(of=Investment)
    )
    investment = db.execute(inv_stmt).scalars().first()
    if not investment:
        raise HTTPException(status_code=404, detail="Apoio não encontrado.")

    offer = investment.offer
    tournament = offer.tournament
    
    # Recalcula percentual
    buyin = Decimal(str(tournament.buyin))
    markup = Decimal(str(offer.markup))
    if buyin <= 0 or markup <= 0:
        raise HTTPException(status_code=400, detail="Dados da oferta inválidos.")

    new_share_pct = q_pct((new_amount / (buyin * markup)) * Decimal("100"))
    
    # Verifica disponibilidade
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

    # Ajuste de carteira do BACKER (não do admin)
    diff = q_money(new_amount - investment.valor_investido)
    backer_wallet = get_wallet_for_update(db, investment.backer_id)
    
    if diff > 0 and backer_wallet.saldo_disponivel < diff:
        raise HTTPException(status_code=400, detail="Saldo do apoiador insuficiente.")

    backer_wallet.saldo_disponivel -= diff
    backer_wallet.saldo_em_jogo += diff
    
    investment.valor_investido = new_amount
    investment.pct_comprada = new_share_pct

    # Sincroniza vendido_pct na oferta
    total_sold_pct = db.execute(
        select(func.coalesce(func.sum(Investment.pct_comprada), 0))
        .where(Investment.offer_id == offer.id)
    ).scalar_one()
    offer.vendido_pct = q_pct(Decimal(str(total_sold_pct or 0)))
    
    # Sincroniza meta atingida
    from routers.escrow import sync_offer_escrow
    sync_offer_escrow(db, offer)

    db.commit()
    return {"success": True}
