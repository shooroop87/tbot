"""Тест: какие акции возвращает API."""
import asyncio
import os
from t_tech.invest import AsyncClient, InstrumentStatus
from t_tech.invest.constants import INVEST_GRPC_API

TOKEN = os.getenv("TINKOFF_TOKEN")

async def main():
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as services:
        resp = await services.instruments.shares(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
        
        # Группируем по exchange и currency
        by_exchange = {}
        for s in resp.instruments:
            key = (s.exchange, s.currency)
            by_exchange[key] = by_exchange.get(key, 0) + 1
        
        print("=" * 50)
        print("АКЦИИ ПО БИРЖАМ И ВАЛЮТАМ:")
        print("=" * 50)
        for k, v in sorted(by_exchange.items(), key=lambda x: -x[1]):
            print(f"  {k[0]:20} {k[1]:5} → {v} шт")
        
        print(f"\nВсего акций: {len(resp.instruments)}")
        
        # RUB акции
        rub = [s for s in resp.instruments if s.currency == "rub"]
        print(f"\n{'=' * 50}")
        print(f"RUB АКЦИИ: {len(rub)} шт")
        print("=" * 50)
        for s in rub[:20]:
            print(f"  {s.ticker:10} | {s.exchange:15} | {s.name[:30]}")
        
        if len(rub) > 20:
            print(f"  ... и ещё {len(rub) - 20}")

if __name__ == "__main__":
    asyncio.run(main())
