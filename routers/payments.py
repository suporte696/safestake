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
from services.mercadopago_service import create_mp_preference, get_mp_payment

router = APIRouter()

MIN_DEPOSIT_AMOUNT = Decimal("5.00")
MAX_DEPOSIT_AMOUNT = Decimal("50000.00")


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


def _extract_payment_id(payload: dict[str, Any], request: Request) -> str | None:
    query_candidates = (
        request.query_params.get("data.id"),
        request.query_params.get("id"),
        request.query_params.get("payment_id"),
    )
    for candidate in query_candidates:
        if candidate:
            return str(candidate)

    direct_candidates = (
        payload.get("data.id"),
        payload.get("id"),
        payload.get("payment_id"),
    )
    for candidate in direct_candidates:
        if candidate:
            return str(candidate)

    data = payload.get("data")
    if isinstance(data, dict):
        nested_id = data.get("id")
        if nested_id:
            return str(nested_id)

    resource = payload.get("resource")
    if isinstance(resource, str) and "/" in resource:
        return resource.rsplit("/", 1)[-1].strip() or None

    return None


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


def _extract_and_validate_amount(payload: dict[str, Any]) -> Decimal:
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
    return amount


def _create_pending_tx(db: Session, user_id: int, amount: Decimal) -> str:
    txid = str(uuid.uuid4())
    tx = PixTransaction(
        user_id=user_id,
        order_nsu=txid,
        amount=amount,
        status="PENDING",
    )
    db.add(tx)
    ensure_wallet_for_user(db, user_id, with_lock=False)
    db.commit()
    return txid


@router.post("/api/deposit/infinitepay")
async def deposit_infinitepay(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar depósito.")
    ensure_user_not_blocked(user)

    payload = await request.json()
    amount = _extract_and_validate_amount(payload)
    txid = _create_pending_tx(db, user.id, amount)
    base_url = str(request.base_url).rstrip("/")

    try:
        redirect_url = f"{base_url}/dashboard?payment=success"
        webhook_url = f"{base_url}/webhooks/infinitepay"
        checkout_url = await generate_checkout_link(
            amount_brl=float(amount),
            order_nsu=txid,
            redirect_url=redirect_url,
            webhook_url=webhook_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar checkout na InfinitePay: {exc}") from exc
    return {"checkout_url": checkout_url, "gateway": "infinitepay"}


@router.post("/api/deposit/mercadopago")
async def deposit_mercadopago(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar depósito.")
    ensure_user_not_blocked(user)

    payload = await request.json()
    amount = _extract_and_validate_amount(payload)
    txid = _create_pending_tx(db, user.id, amount)
    base_url = str(request.base_url).rstrip("/")
    try:
        checkout_url = await create_mp_preference(
            amount_brl=float(amount),
            txid=txid,
            base_url=base_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar checkout no Mercado Pago: {exc}") from exc
    return {"checkout_url": checkout_url, "gateway": "mercadopago"}


@router.post("/webhooks/infinitepay")
async def infinitepay_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        return {"status": "ok"}

    order_nsu = _extract_webhook_order_nsu(payload)
    if not order_nsu:
        return {"status": "ok"}
    mapped_status = _map_webhook_status(payload)

    try:
        with db.begin():
            tx_stmt = select(PixTransaction).where(PixTransaction.order_nsu == order_nsu).with_for_update()
            tx = db.execute(tx_stmt).scalars().first()
            if not tx:
                return {"status": "ok"}

            if mapped_status == "PAID" and tx.status != "PAID":
                tx.status = "PAID"
                wallet = ensure_wallet_for_user(db, tx.user_id, with_lock=True)
                wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount))
    except Exception:
        return {"status": "ok"}

    return {"status": "ok"}


@router.post("/webhooks/mercadopago")
async def mercadopago_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

        payment_id = _extract_payment_id(payload, request)
        if not payment_id:
            return {"status": "ok"}

        payment_response = await get_mp_payment(payment_id)
        payment_data = (payment_response or {}).get("response") or {}
        payment_status = str(payment_data.get("status") or "").strip().lower()
        if payment_status != "approved":
            return {"status": "ok"}

        txid = str(payment_data.get("external_reference") or "").strip()
        if not txid:
            return {"status": "ok"}

        with db.begin():
            tx_stmt = select(PixTransaction).where(PixTransaction.order_nsu == txid).with_for_update()
            tx = db.execute(tx_stmt).scalars().first()
            if not tx:
                return {"status": "ok"}

            if tx.status != "PAID":
                tx.status = "PAID"
                wallet = ensure_wallet_for_user(db, tx.user_id, with_lock=True)
                wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount))
    except Exception:
        return {"status": "ok"}

    return {"status": "ok"}
