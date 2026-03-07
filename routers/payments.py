import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
import os
import logging
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_db
from models import PixTransaction, Wallet
from routers.auth import ensure_user_not_blocked, fetch_current_user
from services.infinitepay import generate_checkout_link
from services.mercadopago_service import (
    create_mp_preference,
    get_mp_merchant_order,
    get_mp_payment,
    search_mp_payment_by_external_reference,
)
from services.ptax import get_usd_brl_ptax_rate

router = APIRouter()
logger = logging.getLogger(__name__)

MONEY_Q = Decimal("0.01")
MIN_DEPOSIT_AMOUNT_USD = Decimal("1.00")
MAX_DEPOSIT_AMOUNT_USD = Decimal("10000.00")


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


def _extract_payment_id(payload: dict[str, Any], request: Request, topic: str = "") -> str | None:
    normalized_topic = (topic or "").lower()
    is_payment_topic = "payment" in normalized_topic
    is_order_topic = "merchant_order" in normalized_topic or normalized_topic.startswith("order")

    query_candidates = [
        request.query_params.get("data.id"),
        request.query_params.get("payment_id"),
    ]
    if is_payment_topic and not is_order_topic:
        query_candidates.append(request.query_params.get("id"))

    for candidate in query_candidates:
        if candidate:
            return str(candidate)

    direct_candidates = [
        payload.get("data.id"),
        payload.get("payment_id"),
    ]
    payload_type = str(payload.get("type") or "").lower()
    payload_action = str(payload.get("action") or "").lower()
    if is_payment_topic or payload_type == "payment" or payload_action.startswith("payment."):
        direct_candidates.append(payload.get("id"))
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


def _parse_mp_signature(signature_header: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in signature_header.split(","):
        part = item.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _extract_notification_data_id(payload: dict[str, Any], request: Request) -> str | None:
    query_candidates = (
        request.query_params.get("data.id"),
        request.query_params.get("data[id]"),
        request.query_params.get("id"),
        request.query_params.get("payment_id"),
        request.query_params.get("merchant_order_id"),
    )
    for candidate in query_candidates:
        if candidate:
            return str(candidate)

    data = payload.get("data")
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])

    if payload.get("id"):
        return str(payload["id"])
    return None


def _is_valid_mp_signature(request: Request, payload: dict[str, Any]) -> bool:
    secret = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "").strip()
    if not secret:
        return True

    signature_header = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    data_id = _extract_notification_data_id(payload, request)
    if not signature_header or not request_id or not data_id:
        logger.warning(
            "MP webhook signature validation failed: missing fields signature=%s request_id=%s data_id=%s query=%s",
            bool(signature_header),
            bool(request_id),
            bool(data_id),
            str(request.query_params),
        )
        return False

    signature_parts = _parse_mp_signature(signature_header)
    ts = signature_parts.get("ts")
    v1 = signature_parts.get("v1")
    if not ts or not v1:
        logger.warning("MP webhook signature validation failed: invalid x-signature format")
        return False

    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    expected = hmac.new(
        secret.encode("utf-8"),
        msg=manifest.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, v1)


def _extract_notification_topic(payload: dict[str, Any], request: Request) -> str:
    raw_topic = (
        request.query_params.get("topic")
        or request.query_params.get("type")
        or payload.get("topic")
        or payload.get("type")
        or payload.get("action")
        or ""
    )
    return str(raw_topic).strip().lower()


def _extract_merchant_order_id(payload: dict[str, Any], request: Request) -> str | None:
    query_candidates = (
        request.query_params.get("data.id"),
        request.query_params.get("id"),
        request.query_params.get("merchant_order_id"),
    )
    for candidate in query_candidates:
        if candidate:
            return str(candidate)

    direct_candidates = (
        payload.get("data.id"),
        payload.get("id"),
        payload.get("merchant_order_id"),
    )
    for candidate in direct_candidates:
        if candidate:
            return str(candidate)

    data = payload.get("data")
    if isinstance(data, dict):
        nested_id = data.get("id")
        if nested_id:
            return str(nested_id)
    return None


def _extract_payment_id_from_merchant_order(response: dict[str, Any]) -> str | None:
    order_data = (response or {}).get("response") or {}
    if not isinstance(order_data, dict):
        return None
    payments = order_data.get("payments")
    if not isinstance(payments, list):
        return None
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        pid = payment.get("id")
        if pid:
            return str(pid)
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
        raise HTTPException(status_code=400, detail="Informe o valor do depósito para continuar.")

    try:
        amount = Decimal(str(amount_raw))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Valor de depósito inválido. Use apenas números.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="O valor de depósito deve ser maior que zero.")
    if amount < MIN_DEPOSIT_AMOUNT_USD:
        raise HTTPException(status_code=400, detail=f"Depósito mínimo é US$ {MIN_DEPOSIT_AMOUNT_USD}.")
    if amount > MAX_DEPOSIT_AMOUNT_USD:
        raise HTTPException(status_code=400, detail=f"Depósito máximo é US$ {MAX_DEPOSIT_AMOUNT_USD}.")
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


def _q_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


async def _convert_usd_to_brl(amount_usd: Decimal) -> tuple[Decimal, Decimal]:
    rate = await get_usd_brl_ptax_rate()
    amount_brl = _q_money(amount_usd * rate)
    return amount_brl, rate


@router.get("/api/deposit/quote")
async def deposit_quote(
    request: Request,
    amount: float = Query(..., gt=0),
    db: Session = Depends(get_db),
):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para consultar a cotação.")
    ensure_user_not_blocked(user)

    try:
        amount_usd = Decimal(str(amount))
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Valor inválido para cotação.") from exc
    if amount_usd <= 0:
        raise HTTPException(status_code=400, detail="Informe um valor maior que zero.")

    try:
        amount_brl, ptax_rate = await _convert_usd_to_brl(amount_usd)
    except Exception as exc:
        logger.exception("Falha ao obter cotação de depósito: %s", exc)
        raise HTTPException(status_code=502, detail="Não foi possível obter a cotação agora.") from exc

    return {
        "amount_usd": float(_q_money(amount_usd)),
        "amount_brl": float(amount_brl),
        "ptax_rate": float(ptax_rate),
    }


def _resolve_base_url(request: Request) -> str:
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base_url:
        return public_base_url
    return str(request.base_url).rstrip("/")


def _credit_tx_wallet_if_needed(db: Session, tx: PixTransaction) -> bool:
    if tx.status == "PAID":
        return False
    tx.status = "PAID"
    wallet = ensure_wallet_for_user(db, tx.user_id, with_lock=True)
    wallet.saldo_disponivel = Decimal(str(wallet.saldo_disponivel)) + Decimal(str(tx.amount))
    return True


@router.post("/api/deposit/infinitepay")
async def deposit_infinitepay(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar seu depósito.")
    ensure_user_not_blocked(user)

    payload = await request.json()
    amount_usd = _extract_and_validate_amount(payload)
    txid = _create_pending_tx(db, user.id, amount_usd)
    base_url = _resolve_base_url(request)
    amount_brl, ptax_rate = await _convert_usd_to_brl(amount_usd)

    try:
        redirect_url = f"{base_url}/dashboard?payment=success"
        webhook_url = f"{base_url}/webhooks/infinitepay"
        checkout_url = await generate_checkout_link(
            amount_brl=float(amount_brl),
            order_nsu=txid,
            redirect_url=redirect_url,
            webhook_url=webhook_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar checkout na InfinitePay: {exc}") from exc
    return {
        "checkout_url": checkout_url,
        "gateway": "infinitepay",
        "amount_usd": float(amount_usd),
        "amount_brl": float(amount_brl),
        "ptax_rate": float(ptax_rate),
    }


@router.post("/api/deposit/mercadopago")
async def deposit_mercadopago(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para realizar seu depósito.")
    ensure_user_not_blocked(user)

    payload = await request.json()
    amount_usd = _extract_and_validate_amount(payload)
    base_url = _resolve_base_url(request)
    if not base_url.lower().startswith("https://"):
        logger.error("MP deposit blocked: base URL não é HTTPS: '%s'", base_url)
        raise HTTPException(
            status_code=400,
            detail="Para depósitos com Mercado Pago, configure PUBLIC_BASE_URL com uma URL HTTPS (ex: https://seusite.com). Em ambiente local use um túnel (ngrok, etc.) e defina PUBLIC_BASE_URL.",
        )
    try:
        amount_brl, ptax_rate = await _convert_usd_to_brl(amount_usd)
    except Exception as exc:
        logger.exception("Conversão USD/BRL falhou no depósito: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Não foi possível obter a cotação do dólar. Tente novamente em instantes.",
        ) from exc
    try:
        txid = _create_pending_tx(db, user.id, amount_usd)
    except Exception as exc:
        logger.exception("Falha ao registrar transação de depósito: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Não foi possível registrar o depósito. Tente novamente.",
        ) from exc
    logger.info(
        "MP deposit started: user_id=%s txid=%s amount=%s base_url=%s",
        user.id,
        txid,
        amount_usd,
        base_url,
    )
    try:
        checkout_url = await create_mp_preference(
            amount_brl=float(amount_brl),
            txid=txid,
            base_url=base_url,
        )
        logger.info("MP preference created: txid=%s", txid)
    except Exception as exc:
        logger.exception("MP preference create failed: user_id=%s txid=%s", user.id, txid)
        raise HTTPException(
            status_code=502,
            detail=f"Não foi possível abrir o checkout do Mercado Pago. Verifique as credenciais (MERCADOPAGO_*) e a URL pública. Detalhe: {exc!s}",
        ) from exc
    return {
        "checkout_url": checkout_url,
        "gateway": "mercadopago",
        "amount_usd": float(amount_usd),
        "amount_brl": float(amount_brl),
        "ptax_rate": float(ptax_rate),
    }


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

        if not _is_valid_mp_signature(request, payload):
            logger.warning("MP webhook rejected: invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        topic = _extract_notification_topic(payload, request)
        payment_id = _extract_payment_id(payload, request, topic=topic)
        logger.info("MP webhook received: topic=%s payment_id=%s", topic, payment_id)
        if not payment_id and ("merchant_order" in topic or topic.startswith("order")):
            merchant_order_id = _extract_merchant_order_id(payload, request)
            if merchant_order_id:
                merchant_order = await get_mp_merchant_order(merchant_order_id)
                payment_id = _extract_payment_id_from_merchant_order(merchant_order)
                logger.info(
                    "MP webhook merchant_order resolved: merchant_order_id=%s payment_id=%s",
                    merchant_order_id,
                    payment_id,
                )

        if not payment_id:
            logger.warning("MP webhook ignored: payment_id not found")
            return {"status": "ok"}

        payment_response = await get_mp_payment(payment_id)
        payment_data = (payment_response or {}).get("response") or {}
        payment_status = str(payment_data.get("status") or "").strip().lower()
        logger.info("MP payment fetched: payment_id=%s status=%s", payment_id, payment_status)
        if payment_status != "approved":
            return {"status": "ok"}

        txid = str(payment_data.get("external_reference") or "").strip()
        if not txid:
            logger.warning("MP webhook ignored: payment_id=%s external_reference missing", payment_id)
            return {"status": "ok"}

        with db.begin():
            tx_stmt = select(PixTransaction).where(PixTransaction.order_nsu == txid).with_for_update()
            tx = db.execute(tx_stmt).scalars().first()
            if not tx:
                logger.warning("MP webhook tx not found: txid=%s payment_id=%s", txid, payment_id)
                return {"status": "ok"}

            credited = _credit_tx_wallet_if_needed(db, tx)
            if credited:
                logger.info(
                    "MP payment credited: txid=%s payment_id=%s user_id=%s amount=%s",
                    txid,
                    payment_id,
                    tx.user_id,
                    tx.amount,
                )
            else:
                logger.info("MP webhook duplicate ignored: txid=%s payment_id=%s", txid, payment_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("MP webhook processing error")
        return {"status": "ok"}

    return {"status": "ok"}


@router.post("/api/deposit/mercadopago/reconcile")
async def reconcile_mercadopago_payment(request: Request, db: Session = Depends(get_db)):
    user = fetch_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Faça login para atualizar o status do depósito.")
    ensure_user_not_blocked(user)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    payment_id = str(payload.get("payment_id") or "").strip()
    provided_txid = str(payload.get("txid") or payload.get("external_reference") or "").strip()
    if not payment_id and not provided_txid:
        raise HTTPException(status_code=400, detail="Informe payment_id ou txid para atualizar o depósito.")

    if not payment_id and provided_txid:
        payment_match = await search_mp_payment_by_external_reference(provided_txid)
        if payment_match and payment_match.get("id"):
            payment_id = str(payment_match["id"]).strip()
        else:
            raise HTTPException(
                status_code=404,
                detail="Não foi possível localizar pagamento no Mercado Pago para o txid informado.",
            )

    payment_response = await get_mp_payment(payment_id)
    payment_data = (payment_response or {}).get("response") or {}
    payment_status = str(payment_data.get("status") or "").strip().lower()
    if payment_status != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"Pagamento ainda não aprovado no Mercado Pago (status atual: {payment_status or 'desconhecido'}).",
        )

    txid = str(payment_data.get("external_reference") or "").strip()
    if not txid:
        txid = provided_txid
    if not txid:
        raise HTTPException(status_code=400, detail="Não foi possível identificar o pagamento (txid/external_reference).")
    if provided_txid and txid != provided_txid:
        raise HTTPException(status_code=400, detail="O txid informado não corresponde ao pagamento consultado.")

    try:
        tx_stmt = select(PixTransaction).where(PixTransaction.order_nsu == txid).with_for_update()
        tx = db.execute(tx_stmt).scalars().first()
        if not tx:
            raise HTTPException(status_code=404, detail="Não encontramos uma transação local para o txid informado.")
        if user.tipo != "admin" and tx.user_id != user.id:
            raise HTTPException(status_code=403, detail="Você não tem permissão para atualizar esta transação.")

        credited = _credit_tx_wallet_if_needed(db, tx)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("MP manual reconcile failed: txid=%s payment_id=%s", txid, payment_id)
        raise HTTPException(status_code=500, detail="Falha ao reconciliar pagamento.") from None

    logger.info(
        "MP manual reconcile: user_id=%s txid=%s payment_id=%s credited=%s",
        user.id,
        txid,
        payment_id,
        credited,
    )
    return {
        "success": True,
        "txid": txid,
        "payment_id": payment_id,
        "transaction_status": "PAID" if credited else "PAID",
        "credited_now": credited,
    }
