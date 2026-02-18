from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import CryptoTransaction, Wallet
from routers.auth import ensure_user_not_blocked, fetch_current_user
from services.coingate import create_order, get_order, map_coingate_status, verify_signature

router = APIRouter()

MIN_DEPOSIT_AMOUNT = Decimal("20.00")
MAX_DEPOSIT_AMOUNT = Decimal("50000.00")


def serialize_crypto_tx(tx: CryptoTransaction) -> dict:
    return {
        "id": tx.id,
        "order_id": tx.coingate_order_id,
        "amount_fiat": float(tx.amount_fiat),
        "amount_crypto": float(tx.amount_crypto) if tx.amount_crypto is not None else None,
        "currency_crypto": tx.currency_crypto,
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


def apply_transaction_status(payload: dict, tx: CryptoTransaction, db: Session) -> None:
    mapped_status = map_coingate_status(str(payload.get("status", "")))
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

    # Idempotência: credita carteira apenas na primeira transição para PAID
    if mapped_status == "PAID" and old_status != "PAID":
        wallet = ensure_wallet_for_user(db, tx.user_id, with_lock=True)
        wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount_fiat))


@router.post("/api/deposit/crypto")
async def deposit_crypto(request: Request, db: Session = Depends(get_db)):
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

    try:
        order_data = await create_order(amount_brl=float(amount), user_email=user.email)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao criar pedido CoinGate: {exc}") from exc

    tx = CryptoTransaction(
        user_id=user.id,
        coingate_order_id=str(order_data["order_id"]),
        amount_fiat=amount,
        status="PENDING",
    )
    db.add(tx)
    ensure_wallet_for_user(db, user.id, with_lock=False)
    db.commit()
    db.refresh(tx)
    return {
        "payment_url": order_data["payment_url"],
        "order_id": order_data["order_id"],
        "transaction": serialize_crypto_tx(tx),
    }


@router.get("/api/deposit/crypto/history")
def deposit_crypto_history(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para visualizar transações.")
    ensure_user_not_blocked(user)
    stmt = (
        select(CryptoTransaction)
        .where(CryptoTransaction.user_id == user.id)
        .order_by(CryptoTransaction.created_at.desc(), CryptoTransaction.id.desc())
    )
    transactions = [serialize_crypto_tx(tx) for tx in db.execute(stmt).scalars().all()]
    return {"transactions": transactions}


@router.post("/api/deposit/crypto/{order_id}/refresh")
async def refresh_deposit_status(order_id: str, request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para atualizar transações.")
    ensure_user_not_blocked(user)

    tx_stmt = (
        select(CryptoTransaction)
        .where(CryptoTransaction.coingate_order_id == str(order_id), CryptoTransaction.user_id == user.id)
        .with_for_update()
    )
    tx = db.execute(tx_stmt).scalars().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transação não encontrada.")

    try:
        order_data = await get_order(str(order_id))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar pedido CoinGate: {exc}") from exc

    apply_transaction_status(order_data, tx, db)
    db.commit()
    db.refresh(tx)
    return {"transaction": serialize_crypto_tx(tx)}


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

    with db.begin():
        tx_stmt = (
            select(CryptoTransaction)
            .where(CryptoTransaction.coingate_order_id == str(order_id))
            .with_for_update()
        )
        tx = db.execute(tx_stmt).scalars().first()
        if not tx:
            return {"received": True, "ignored": True}

        apply_transaction_status(payload, tx, db)

    return {"received": True, "status": tx.status}
