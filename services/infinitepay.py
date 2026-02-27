import json
import os
from typing import Any

import httpx


def _get_api_url() -> str:
    return os.getenv("INFINITEPAY_API_URL", "https://api.infinitepay.io").rstrip("/")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text or "sem detalhes no corpo da resposta"
    return json.dumps(data, ensure_ascii=False)


def _pick_first(data: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value:
            return value
    return None


def _get_checkout_path() -> str:
    return os.getenv(
        "INFINITEPAY_CHECKOUT_PATH",
        "/invoices/public/checkout/links",
    ).strip()


async def generate_checkout_link(
    amount_brl: float,
    order_nsu: str,
    redirect_url: str,
    webhook_url: str,
) -> str:
    amount_cents = int(amount_brl * 100)
    if amount_cents <= 0:
        raise RuntimeError("Valor para checkout deve ser maior que zero.")

    handle = os.getenv("INFINITEPAY_RECEIVER_HANDLE", "jeova-enderson").strip()
    payload = {
        "handle": handle,
        "order_nsu": order_nsu,
        "items": [
            {
                "quantity": 1,
                "price": amount_cents,
                "description": "Depósito Safe Stake",
            }
        ],
        "redirect_url": redirect_url,
        "webhook_url": webhook_url,
    }

    url = f"{_get_api_url()}{_get_checkout_path()}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(url, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_error_detail(exc.response)
            raise RuntimeError(f"InfinitePay retornou {exc.response.status_code}: {detail}") from exc
        data = response.json()

    checkout_url = _pick_first(
        data,
        ["url", "checkout_url", "payment_url", "link"],
    )
    if not checkout_url:
        raise RuntimeError("InfinitePay não retornou URL de checkout.")
    return str(checkout_url)
