"""Тест: проверка фильтра свечей по часам."""
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
        
        # Берём SBER или любую ликвидную акцию
        resp = await services.instruments.shares(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
        
        # Ищем Магнит
        share = None
        for s in resp.instruments:
            if s.ticker == "MGNT":
                share = s
                break
        
        if not share:
            print("MGNT не найден!")
            return
        
        print(f"Тикер: {share.ticker}, FIGI: {share.figi}")
        print("=" * 60)
        
        # Загружаем ЧАСОВЫЕ свечи за 35 дней
        from_dt = datetime.utcnow() - timedelta(days=35)
        to_dt = datetime.utcnow()
        
        candles_resp = await services.market_data.get_candles(
            figi=share.figi,
            from_=from_dt,
            to=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
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
        
        print(f"Загружено часовых свечей: {len(candles)}")
        
        if not candles:
            print("Нет свечей!")
            return
        
        df = pd.DataFrame(candles)
        
        # Смотрим какое время приходит
        print("\nПервые 10 свечей (сырые данные):")
        for i, row in df.head(10).iterrows():
            t = row["time"]
            print(f"  {t} | tz={t.tzinfo} | close={row['close']}")
        
        # Конвертируем в МСК
        df["time"] = pd.to_datetime(df["time"])
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        df["time_msk"] = df["time"].dt.tz_convert(MSK)
        df["hour"] = df["time_msk"].dt.hour
        df["weekday"] = df["time_msk"].dt.weekday
        df["date"] = df["time_msk"].dt.date
        
        print("\nПервые 10 свечей (с МСК):")
        for i, row in df.head(10).iterrows():
            print(f"  {row['time_msk']} | hour={row['hour']} | weekday={row['weekday']}")
        
        # Распределение по часам
        print("\nРаспределение по часам МСК:")
        hour_counts = df["hour"].value_counts().sort_index()
        for hour, count in hour_counts.items():
            marker = " ← торговые" if 10 <= hour < 19 else ""
            print(f"  {hour:02d}:00 = {count} свечей{marker}")
        
        # Фильтруем 10-19
        df_filtered = df[(df["hour"] >= 10) & (df["hour"] < 19) & (df["weekday"] < 5)]
        print(f"\nПосле фильтра 10-19 МСК (пн-пт): {len(df_filtered)} свечей")
        
        if len(df_filtered) == 0:
            print("ПРОБЛЕМА: фильтр отсёк всё!")
            return
        
        # Группируем по дням → дневные свечи
        daily = df_filtered.groupby("date").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).reset_index()
        
        print(f"\nПолучено дневных свечей: {len(daily)}")
        print("\nПоследние 10 дней:")
        print(daily.tail(10).to_string(index=False))


if __name__ == "__main__":
    asyncio.run(main())