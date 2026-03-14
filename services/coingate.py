import hashlib
import hmac
import json
import os
import secrets
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

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


def _resolve_env_value(name: str, default: str) -> str:
    return os.path.expandvars(os.getenv(name, default)).strip()


def _get_redirect_url(name: str, default: str) -> str:
    value = _resolve_env_value(name, default)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"{name} inválida: configure uma URL absoluta (http/https).")
    return value


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text or "sem detalhes no corpo da resposta"
    return json.dumps(data, ensure_ascii=False)


def _fetch_brl_conversion_rate(target_currency: str) -> Decimal:
    with httpx.Client(timeout=10.0) as client:
        response = client.get("https://open.er-api.com/v6/latest/BRL")
        response.raise_for_status()
        data = response.json()
    if str(data.get("result", "")).lower() != "success":
        raise RuntimeError("serviço de câmbio retornou resposta inválida")
    rates = data.get("rates")
    if not isinstance(rates, dict):
        raise RuntimeError("serviço de câmbio sem tabela de taxas")
    rate_raw = rates.get(target_currency)
    if rate_raw is None:
        raise RuntimeError(f"taxa BRL->{target_currency} indisponível")
    try:
        rate = Decimal(str(rate_raw))
    except Exception as exc:
        raise RuntimeError("taxa de câmbio inválida") from exc
    if rate <= 0:
        raise RuntimeError("taxa de câmbio não positiva")
    return rate


def _resolve_price_from_brl(amount_brl: float) -> tuple[str, str]:
    target_currency = os.getenv("COINGATE_PRICE_CURRENCY", "USD").strip().upper()
    amount_brl_decimal = Decimal(str(amount_brl))
    if target_currency == "BRL":
        # CoinGate sandbox não aceita BRL como moeda de preço.
        raise RuntimeError("COINGATE_PRICE_CURRENCY=BRL não é suportada pela CoinGate. Use USD, EUR ou GBP.")

    if target_currency:
        explicit_rate = os.getenv(f"COINGATE_BRL_TO_{target_currency}_RATE")
        if explicit_rate:
            try:
                rate = Decimal(str(explicit_rate))
            except Exception as exc:
                raise RuntimeError(f"Taxa COINGATE_BRL_TO_{target_currency}_RATE inválida.") from exc
            if rate <= 0:
                raise RuntimeError(f"Taxa COINGATE_BRL_TO_{target_currency}_RATE deve ser maior que zero.")
        else:
            try:
                rate = _fetch_brl_conversion_rate(target_currency)
            except Exception as exc:
                raise RuntimeError(
                    f"Não foi possível converter BRL para {target_currency}. Configure "
                    f"COINGATE_BRL_TO_{target_currency}_RATE para fallback manual."
                ) from exc
        converted = (amount_brl_decimal * rate).quantize(Decimal("0.01"))
        return str(converted), target_currency

    raise RuntimeError("COINGATE_PRICE_CURRENCY não configurada.")


async def create_order(amount_brl: float, user_email: str) -> dict[str, Any]:
    base_url = _resolve_env_value("BASE_URL", "http://localhost:8000")
    callback_url = _get_redirect_url("COINGATE_CALLBACK_URL", f"{base_url}/webhooks/coingate")
    success_url = _get_redirect_url("COINGATE_SUCCESS_URL", f"{base_url}/dashboard")
    cancel_url = _get_redirect_url("COINGATE_CANCEL_URL", f"{base_url}/dashboard")
    price_amount, price_currency = _resolve_price_from_brl(amount_brl)
    payload = {
        "price_amount": price_amount,
        "price_currency": price_currency,
        "receive_currency": os.getenv("COINGATE_RECEIVE_CURRENCY", "DO_NOT_CONVERT"),
        "order_id": f"safe-stake-{secrets.token_hex(4)}-{price_amount.replace('.', '')}",
        "callback_url": callback_url,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "title": "SAFE STAKE - Crypto Deposit",
        "description": f"Depósito cripto SAFE STAKE para {user_email}",
        "purchaser_email": user_email,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{_get_api_url()}/orders", headers=_build_headers(), json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_error_detail(exc.response)
            raise RuntimeError(f"CoinGate retornou {exc.response.status_code}: {detail}") from exc
        data = response.json()

    payment_url = data.get("payment_url") or data.get("payment_url_primary")
    if not payment_url:
        raise RuntimeError("CoinGate não retornou payment_url.")
    if "id" not in data:
        raise RuntimeError("CoinGate não retornou id do pedido.")

    return {"order_id": str(data["id"]), "payment_url": payment_url, "raw": data}


async def get_order(order_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{_get_api_url()}/orders/{order_id}", headers=_build_headers())
        response.raise_for_status()
        return response.json()


def map_coingate_status(status_raw: str | None) -> str:
    normalized = str(status_raw or "").strip().lower()
    if normalized in {"paid", "confirmed"}:
        return "PAID"
    if normalized in {"expired", "canceled", "cancelled", "invalid"}:
        return "EXPIRED"
    return "PENDING"


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
    normalized = signature.strip()
    if normalized.lower().startswith("sha256="):
        normalized = normalized.split("=", maxsplit=1)[1]
    return hmac.compare_digest(expected, normalized)
