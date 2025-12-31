"""Тест: сравнение BB для UWGN с терминалом."""
import asyncio
import os
from datetime import datetime, timedelta

import pandas as pd
import pytz
from t_tech.invest import AsyncClient, CandleInterval, InstrumentStatus
from t_tech.invest.constants import INVEST_GRPC_API
from t_tech.invest.utils import quotation_to_decimal

TOKEN = os.getenv("TINKOFF_TOKEN")
MSK = pytz.timezone("Europe/Moscow")


async def main():
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as services:
        
        # Ищем UWGN
        resp = await services.instruments.shares(instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE)
        share = next((s for s in resp.instruments if s.ticker == "UWGN"), None)
        
        if not share:
            print("UWGN не найден!")
            return
        
        print(f"UWGN FIGI: {share.figi}")
        print("=" * 60)
        
        # Загружаем ЧАСОВЫЕ свечи
        candles_resp = await services.market_data.get_candles(
            figi=share.figi,
            from_=datetime.utcnow() - timedelta(days=35),
            to=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        candles = [{
            "time": c.time,
            "open": float(quotation_to_decimal(c.open)),
            "high": float(quotation_to_decimal(c.high)),
            "low": float(quotation_to_decimal(c.low)),
            "close": float(quotation_to_decimal(c.close)),
        } for c in candles_resp.candles if c.is_complete]
        
        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"])
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        df["time_msk"] = df["time"].dt.tz_convert(MSK)
        df["hour"] = df["time_msk"].dt.hour
        df["date"] = df["time_msk"].dt.date
        
        print(f"Часовых свечей: {len(df)}")
        
        # Вариант 1: Агрегация 10-19 МСК (наш метод)
        df_filtered = df[(df["hour"] >= 10) & (df["hour"] < 19)]
        daily_10_19 = df_filtered.groupby("date").agg({
            "open": "first", "high": "max", "low": "min", "close": "last"
        }).reset_index()
        
        # Вариант 2: Агрегация ВСЕХ часов (как терминал)
        daily_all = df.groupby("date").agg({
            "open": "first", "high": "max", "low": "min", "close": "last"
        }).reset_index()
        
        # Вариант 3: Дневные свечи напрямую из API
        candles_day = await services.market_data.get_candles(
            figi=share.figi,
            from_=datetime.utcnow() - timedelta(days=35),
            to=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        daily_api = pd.DataFrame([{
            "date": c.time.date(),
            "open": float(quotation_to_decimal(c.open)),
            "high": float(quotation_to_decimal(c.high)),
            "low": float(quotation_to_decimal(c.low)),
            "close": float(quotation_to_decimal(c.close)),
        } for c in candles_day.candles if c.is_complete])
        
        def calc_bb(df, label):
            """Считает BB(20, 2) и выводит результат."""
            if len(df) < 20:
                print(f"{label}: недостаточно данных ({len(df)} дней)")
                return
            
            df = df.copy().sort_values("date").reset_index(drop=True)
            middle = df["close"].rolling(20).mean().iloc[-1]
            std = df["close"].rolling(20).std().iloc[-1]
            upper = middle + 2 * std
            lower = middle - 2 * std
            last_close = df["close"].iloc[-1]
            
            print(f"\n{label}:")
            print(f"  Дней: {len(df)}")
            print(f"  Последний close: {last_close:.2f}")
            print(f"  BB Upper: {upper:.2f}")
            print(f"  BB Middle: {middle:.2f}")
            print(f"  BB Lower: {lower:.2f}")
            print(f"  Последние 5 close: {list(df['close'].tail(5).round(2))}")
        
        calc_bb(daily_10_19, "Вариант 1: Агрегация 10-19 МСК")
        calc_bb(daily_all, "Вариант 2: Агрегация всех часов")
        calc_bb(daily_api, "Вариант 3: Дневные свечи из API")
        
        print("\n" + "=" * 60)
        print("Терминал показывает: BB Lower = 29.79")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())