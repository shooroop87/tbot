"""Тест: загрузка свечей и расчёт ATR."""
import asyncio
import os
from datetime import datetime, timedelta
from t_tech.invest import AsyncClient, CandleInterval, InstrumentStatus
from t_tech.invest.constants import INVEST_GRPC_API
from t_tech.invest.utils import quotation_to_decimal
import pandas as pd

TOKEN = os.getenv("TINKOFF_TOKEN")

async def main():
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as services:
        
        # Находим Si
        resp = await services.instruments.futures(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
        
        now = datetime.now()
        si = None
        for f in resp.instruments:
            if f.ticker.startswith("Si"):
                exp = f.expiration_date.replace(tzinfo=None)
                if exp > now:
                    si = f
                    break
        
        if not si:
            print("Si не найден!")
            return
        
        print(f"Тикер: {si.ticker}, FIGI: {si.figi}")
        print("=" * 50)
        
        # Загружаем ДНЕВНЫЕ свечи за 30 дней
        from_dt = datetime.utcnow() - timedelta(days=30)
        to_dt = datetime.utcnow()
        
        candles_resp = await services.market_data.get_candles(
            figi=si.figi,
            from_=from_dt,
            to=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        
        candles = []
        for c in candles_resp.candles:
            if c.is_complete:
                candles.append({
                    "time": c.time,
                    "open": float(quotation_to_decimal(c.open)),
                    "high": float(quotation_to_decimal(c.high)),
                    "low": float(quotation_to_decimal(c.low)),
                    "close": float(quotation_to_decimal(c.close)),
                    "volume": c.volume,
                })
        
        print(f"Загружено дневных свечей: {len(candles)}")
        
        if not candles:
            return
        
        df = pd.DataFrame(candles)
        
        # Расчёт True Range
        df["prev_close"] = df["close"].shift(1)
        df["tr1"] = df["high"] - df["low"]
        df["tr2"] = abs(df["high"] - df["prev_close"])
        df["tr3"] = abs(df["low"] - df["prev_close"])
        df["true_range"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
        
        # ATR(14) - EMA
        df["atr_14"] = df["true_range"].ewm(span=14, adjust=False).mean()
        
        print("\nПоследние 10 дней:")
        print(df[["time", "close", "true_range", "atr_14"]].tail(10).to_string())
        
        last_atr = df["atr_14"].iloc[-1]
        last_close = df["close"].iloc[-1]
        atr_pct = last_atr / last_close * 100
        
        print(f"\n{'=' * 50}")
        print(f"ATR(14) = {last_atr:.0f} пунктов ({atr_pct:.2f}%)")
        print(f"Последняя цена: {last_close:.0f}")

if __name__ == "__main__":
    asyncio.run(main())