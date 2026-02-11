import hashlib
import hmac
import json
import os
from decimal import Decimal
from typing import Any

import httpx


def _get_api_url() -> str:
    return os.getenv("COINGATE_API_URL", "https://api-sandbox.coingate.com/v2").rstrip("/")


def _get_auth_token() -> str:
    token = os.getenv("COINGATE_AUTH_TOKEN")
    if not token:
        raise RuntimeError("COINGATE_AUTH_TOKEN não configurado.")
    return token


def _build_headers() -> dict[str, str]:
    return {
        "Authorization": f"Token {_get_auth_token()}",
        "Content-Type": "application/json",
    }


async def create_order(amount_brl: float, user_email: str) -> dict[str, Any]:
    callback_url = os.getenv("COINGATE_CALLBACK_URL", "http://localhost:8000/webhooks/coingate")
    success_url = os.getenv("COINGATE_SUCCESS_URL", "http://localhost:8000/dashboard")
    cancel_url = os.getenv("COINGATE_CANCEL_URL", "http://localhost:8000/dashboard")
    payload = {
        "price_amount": str(Decimal(str(amount_brl)).quantize(Decimal("0.01"))),
        "price_currency": "BRL",
        "receive_currency": os.getenv("COINGATE_RECEIVE_CURRENCY", "DO_NOT_CONVERT"),
        "callback_url": callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "title": "SAFE STAKE - Crypto Deposit",
        "description": f"Depósito cripto SAFE STAKE para {user_email}",
        "token": user_email,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{_get_api_url()}/orders", headers=_build_headers(), json=payload)
        response.raise_for_status()
        data = response.json()

    payment_url = data.get("payment_url") or data.get("payment_url_primary")
    if not payment_url:
        raise RuntimeError("CoinGate não retornou payment_url.")
    if "id" not in data:
        raise RuntimeError("CoinGate não retornou id do pedido.")

    return {"order_id": str(data["id"]), "payment_url": payment_url, "raw": data}


def verify_signature(payload: Any, signature: str | None) -> bool:
    if not signature:
        return False
    secret = os.getenv("COINGATE_WEBHOOK_SECRET", os.getenv("COINGATE_AUTH_TOKEN", ""))
    if not secret:
        return False

    if isinstance(payload, bytes):
        payload_raw = payload
    elif isinstance(payload, str):
        payload_raw = payload.encode("utf-8")
    else:
        payload_raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload_raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
