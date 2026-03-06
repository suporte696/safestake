from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

BCB_PTAX_ENDPOINT = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarDia(dataCotacao=@dataCotacao)?"
    "@dataCotacao='{date}'&$top=1&$format=json"
)
DEFAULT_USD_BRL_RATE = Decimal("5.00")


def _format_bcb_date(target_date: datetime) -> str:
    return target_date.strftime("%m-%d-%Y")


async def get_usd_brl_ptax_rate() -> Decimal:
    """
    Retorna cotação PTAX de venda USD->BRL.

    Faz fallback para até 7 dias anteriores para cobrir finais de semana/feriados.
    """
    now_utc = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=10.0) as client:
        for day_offset in range(0, 8):
            query_date = now_utc - timedelta(days=day_offset)
            url = BCB_PTAX_ENDPOINT.format(date=_format_bcb_date(query_date))
            try:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
                values = payload.get("value") or []
                if not values:
                    continue
                quote = values[0] or {}
                raw_rate = quote.get("cotacaoVenda") or quote.get("cotacaoCompra")
                if raw_rate is None:
                    continue
                rate = Decimal(str(raw_rate))
                if rate > 0:
                    return rate
            except (httpx.HTTPError, InvalidOperation, ValueError):
                continue
    return DEFAULT_USD_BRL_RATE
