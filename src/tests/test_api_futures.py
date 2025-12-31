"""Тест: фьючерсы Si и другие."""
import asyncio
import os
from datetime import datetime
from t_tech.invest import AsyncClient, InstrumentStatus
from t_tech.invest.constants import INVEST_GRPC_API

TOKEN = os.getenv("TINKOFF_TOKEN")

async def main():
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as services:
        resp = await services.instruments.futures(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
        
        now = datetime.now()
        
        # Фильтруем активные фьючерсы Si
        si_futures = []
        for f in resp.instruments:
            if f.ticker.startswith("Si"):
                exp = f.expiration_date.replace(tzinfo=None)
                if exp > now:
                    si_futures.append(f)
        
        si_futures.sort(key=lambda x: x.expiration_date)
        
        print("=" * 60)
        print("ФЬЮЧЕРСЫ Si (USD/RUB):")
        print("=" * 60)
        for f in si_futures:
            exp = f.expiration_date.strftime("%Y-%m-%d")
            print(f"  {f.ticker:10} | exp: {exp} | figi: {f.figi}")
        
        # Другие популярные фьючерсы
        print(f"\n{'=' * 60}")
        print("ДРУГИЕ ФЬЮЧЕРСЫ (Ri, BR, NG, ED):")
        print("=" * 60)
        
        for prefix in ["Ri", "BR", "NG", "ED"]:
            futures = [f for f in resp.instruments 
                      if f.ticker.startswith(prefix) 
                      and f.expiration_date.replace(tzinfo=None) > now]
            futures.sort(key=lambda x: x.expiration_date)
            
            if futures:
                f = futures[0]  # ближайший
                exp = f.expiration_date.strftime("%Y-%m-%d")
                print(f"  {f.ticker:10} | exp: {exp} | {f.name[:35]}")

if __name__ == "__main__":
    asyncio.run(main())