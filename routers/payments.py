import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import PixTransaction, Wallet
from routers.auth import ensure_user_not_blocked, fetch_current_user
from services.infinitepay import generate_checkout_link

router = APIRouter()

MIN_DEPOSIT_AMOUNT = Decimal("20.00")
MAX_DEPOSIT_AMOUNT = Decimal("50000.00")


def serialize_pix_tx(tx: PixTransaction) -> dict[str, Any]:
    return {
        "id": tx.id,
        "order_nsu": tx.order_nsu,
        "amount": float(tx.amount),
        "status": tx.status,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }


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


def _map_webhook_status(payload: dict[str, Any]) -> str:
    raw_status = str(payload.get("status") or payload.get("event") or "").strip().upper()
    if any(token in raw_status for token in ("PAID", "APPROVED", "CONFIRMED")):
        return "PAID"
    return "PENDING"


def _extract_webhook_order_nsu(payload: dict[str, Any]) -> str | None:
    direct = payload.get("order_nsu")
    if direct:
        return str(direct)
    data = payload.get("data")
    if isinstance(data, dict):
        order_nsu = data.get("order_nsu")
        if order_nsu:
            return str(order_nsu)
        invoice = data.get("invoice")
        if isinstance(invoice, dict) and invoice.get("order_nsu"):
            return str(invoice["order_nsu"])
    invoice = payload.get("invoice")
    if isinstance(invoice, dict) and invoice.get("order_nsu"):
        return str(invoice["order_nsu"])
    return None


@router.post("/api/deposit/infinitepay")
async def deposit_infinitepay(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar depósito.")
    ensure_user_not_blocked(user)

    payload = await request.json()
    amount_raw = payload.get("amount")
    if amount_raw is None:
        raise HTTPException(status_code=400, detail="Campo amount é obrigatório.")

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor de depósito inválido.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Valor deve ser maior que zero.")
    if amount < MIN_DEPOSIT_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Depósito mínimo é R$ {MIN_DEPOSIT_AMOUNT}.")
    if amount > MAX_DEPOSIT_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Depósito máximo é R$ {MAX_DEPOSIT_AMOUNT}.")

    order_nsu = str(uuid.uuid4())
    tx = PixTransaction(
        user_id=user.id,
        order_nsu=order_nsu,
        amount=amount,
        status="PENDING",
    )
    db.add(tx)
    ensure_wallet_for_user(db, user.id, with_lock=False)
    db.commit()

    try:
        base_url = str(request.base_url).rstrip("/")
        redirect_url = f"{base_url}/dashboard?payment=success"
        webhook_url = f"{base_url}/webhooks/infinitepay"
        checkout_url = await generate_checkout_link(
            amount_brl=float(amount),
            order_nsu=order_nsu,
            redirect_url=redirect_url,
            webhook_url=webhook_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar link de pagamento na InfinitePay: {exc}") from exc

    return {
        "transaction": serialize_pix_tx(tx),
        "checkout_url": checkout_url,
    }


@router.post("/webhooks/infinitepay")
async def infinitepay_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        return {"received": True, "ignored": True}

    order_nsu = _extract_webhook_order_nsu(payload)
    if not order_nsu:
        return {"received": True, "ignored": True}
    mapped_status = _map_webhook_status(payload)

    with db.begin():
        tx_stmt = select(PixTransaction).where(PixTransaction.order_nsu == order_nsu).with_for_update()
        tx = db.execute(tx_stmt).scalars().first()
        if not tx:
            return {"received": True, "ignored": True}

        old_status = tx.status
        tx.status = mapped_status

        if mapped_status == "PAID" and old_status != "PAID":
            wallet = ensure_wallet_for_user(db, tx.user_id, with_lock=True)
            wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount))

    return {"received": True, "status": mapped_status}
