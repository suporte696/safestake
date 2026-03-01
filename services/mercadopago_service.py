import os
from typing import Any
from urllib.parse import quote_plus
from urllib.parse import urlparse

import mercadopago
from fastapi.concurrency import run_in_threadpool


def _get_sdk() -> mercadopago.SDK:
    token = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Variável de ambiente MERCADOPAGO_ACCESS_TOKEN não configurada.")
    return mercadopago.SDK(token)


def _normalize_response(raw_response: Any) -> dict[str, Any]:
    if isinstance(raw_response, dict):
        return raw_response
    if hasattr(raw_response, "__dict__"):
        data = dict(vars(raw_response))
        for attr in ("status", "response", "message", "error", "cause"):
            if attr not in data and hasattr(raw_response, attr):
                data[attr] = getattr(raw_response, attr)
        return data
    return {}


def _pick_checkout_url(response: dict[str, Any]) -> str | None:
    payload = response or {}
    nested = payload.get("response")
    if isinstance(nested, dict):
        payload = nested
    elif not isinstance(payload, dict):
        payload = {}

    for key in ("init_point", "sandbox_init_point"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _pick_preference_id(response: dict[str, Any]) -> str | None:
    payload = response or {}
    nested = payload.get("response")
    if isinstance(nested, dict):
        payload = nested
    elif not isinstance(payload, dict):
        payload = {}

    pref_id = payload.get("id")
    if pref_id:
        return str(pref_id)
    return None


def _build_checkout_url_from_preference_id(preference_id: str) -> str:
    base = os.getenv(
        "MERCADOPAGO_CHECKOUT_REDIRECT_BASE",
        "https://www.mercadopago.com.br/checkout/v1/redirect",
    ).rstrip("/")
    return f"{base}?pref_id={quote_plus(preference_id)}"


def _is_public_https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


async def create_mp_preference(amount_brl: float, txid: str, base_url: str) -> str:
    sdk = _get_sdk()
    preference_data: dict[str, Any] = {
        "items": [
            {
                "title": "Depósito Safe Stake",
                "quantity": 1,
                "unit_price": float(amount_brl),
                "currency_id": "BRL",
            }
        ],
        "external_reference": str(txid),
    }
    if _is_public_https_url(base_url):
        preference_data["back_urls"] = {
            "success": f"{base_url}/dashboard?payment=success",
            "failure": f"{base_url}/dashboard?payment=failure",
            "pending": f"{base_url}/dashboard?payment=pending",
        }
        preference_data["auto_return"] = "approved"
        preference_data["notification_url"] = f"{base_url}/webhooks/mercadopago"

    raw_response = await run_in_threadpool(sdk.preference().create, preference_data)
    response = _normalize_response(raw_response)
    checkout_url = _pick_checkout_url(response)
    if checkout_url:
        return checkout_url

    preference_id = _pick_preference_id(response)
    if preference_id:
        return _build_checkout_url_from_preference_id(preference_id)

    status = (response or {}).get("status")
    cause = (response or {}).get("cause")
    error = (response or {}).get("error")
    message = (response or {}).get("message")
    response_payload = (response or {}).get("response")
    response_keys = list(response_payload.keys()) if isinstance(response_payload, dict) else []
    raise RuntimeError(
        "Mercado Pago não retornou URL nem ID da preferência "
        f"(status={status}, error={error}, message={message}, cause={cause}, response_keys={response_keys})."
    )


async def get_mp_payment(payment_id: str) -> dict[str, Any]:
    sdk = _get_sdk()
    raw_response = await run_in_threadpool(sdk.payment().get, payment_id)
    return _normalize_response(raw_response)
