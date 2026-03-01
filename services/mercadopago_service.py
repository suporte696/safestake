import os
from typing import Any

import mercadopago
from fastapi.concurrency import run_in_threadpool


def _get_sdk() -> mercadopago.SDK:
    token = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Variável de ambiente MERCADOPAGO_ACCESS_TOKEN não configurada.")
    return mercadopago.SDK(token)


async def create_mp_preference(amount_brl: float, txid: str, base_url: str) -> str:
    sdk = _get_sdk()
    preference_data = {
        "items": [
            {
                "title": "Depósito Safe Stake",
                "quantity": 1,
                "unit_price": float(amount_brl),
                "currency_id": "BRL",
            }
        ],
        "external_reference": str(txid),
        "back_urls": {
            "success": f"{base_url}/dashboard?payment=success",
            "failure": f"{base_url}/dashboard?payment=failure",
            "pending": f"{base_url}/dashboard?payment=pending",
        },
        "auto_return": "approved",
        "notification_url": f"{base_url}/webhooks/mercadopago",
    }

    response = await run_in_threadpool(sdk.preference().create, preference_data)
    init_point = ((response or {}).get("response") or {}).get("init_point")
    if not init_point:
        raise RuntimeError("Mercado Pago não retornou init_point da preferência.")
    return str(init_point)


async def get_mp_payment(payment_id: str) -> dict[str, Any]:
    sdk = _get_sdk()
    response = await run_in_threadpool(sdk.payment().get, payment_id)
    return response or {}
