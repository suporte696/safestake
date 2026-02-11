from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import CryptoTransaction, Wallet
from routers.auth import fetch_current_user
from services.coingate import create_order, verify_signature

router = APIRouter()


@router.post("/api/deposit/crypto")
async def deposit_crypto(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar depósito.")

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

    try:
        order_data = await create_order(amount_brl=float(amount), user_email=user.email)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao criar pedido CoinGate: {exc}") from exc

    with db.begin():
        tx = CryptoTransaction(
            user_id=user.id,
            coingate_order_id=str(order_data["order_id"]),
            amount_fiat=amount,
            status="PENDING",
        )
        db.add(tx)

        wallet_stmt = select(Wallet).where(Wallet.user_id == user.id)
        wallet = db.execute(wallet_stmt).scalars().first()
        if not wallet:
            db.add(
                Wallet(
                    user_id=user.id,
                    saldo_disponivel=Decimal("0"),
                    saldo_bloqueado=Decimal("0"),
                    saldo_em_jogo=Decimal("0"),
                )
            )

    return {"payment_url": order_data["payment_url"], "order_id": order_data["order_id"]}


@router.post("/webhooks/coingate")
async def coingate_webhook(request: Request, db: Session = Depends(get_db)):
    raw_payload = await request.body()
    signature = (
        request.headers.get("X-CoinGate-Signature")
        or request.headers.get("Coingate-Signature")
        or request.headers.get("x-coingate-signature")
    )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Payload inválido no webhook.") from exc

    if not verify_signature(raw_payload, signature):
        raise HTTPException(status_code=401, detail="Assinatura inválida.")

    order_id = payload.get("id") or payload.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="Webhook sem id do pedido.")

    status_raw = str(payload.get("status", "")).lower()
    mapped_status = "PENDING"
    if status_raw == "paid":
        mapped_status = "PAID"
    elif status_raw in {"expired", "canceled", "cancelled", "invalid"}:
        mapped_status = "EXPIRED"

    with db.begin():
        tx_stmt = (
            select(CryptoTransaction)
            .where(CryptoTransaction.coingate_order_id == str(order_id))
            .with_for_update()
        )
        tx = db.execute(tx_stmt).scalars().first()
        if not tx:
            return {"received": True, "ignored": True}

        old_status = tx.status
        tx.status = mapped_status

        pay_amount = payload.get("pay_amount")
        if pay_amount is not None:
            try:
                tx.amount_crypto = Decimal(str(pay_amount))
            except InvalidOperation:
                pass
        pay_currency = payload.get("pay_currency")
        if pay_currency:
            tx.currency_crypto = str(pay_currency)

        if mapped_status == "PAID" and old_status != "PAID":
            wallet_stmt = select(Wallet).where(Wallet.user_id == tx.user_id).with_for_update()
            wallet = db.execute(wallet_stmt).scalars().first()
            if not wallet:
                wallet = Wallet(
                    user_id=tx.user_id,
                    saldo_disponivel=Decimal("0"),
                    saldo_bloqueado=Decimal("0"),
                    saldo_em_jogo=Decimal("0"),
                )
                db.add(wallet)
                db.flush()

            wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount_fiat))

    return {"received": True, "status": mapped_status}
