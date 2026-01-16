"""
Планируемые задачи.

DailyCalculationJob: ежедневный расчёт индикаторов в 06:30 МСК
"""
import asyncio
from datetime import datetime, timedelta, date
from typing import List, Dict, Any, Optional

import pytz
import structlog
from t_tech.invest import CandleInterval

from config import Config
from api.tinkoff_client import TinkoffClient
from api.telegram_notifier import TelegramNotifier
from api.telegram_bot import update_shares_cache
from db.repository import Repository
from indicators.daily_aggregator import aggregate_hourly_to_daily, calculate_indicators

logger = structlog.get_logger()
MSK = pytz.timezone("Europe/Moscow")

# Сумма заявки по умолчанию
ORDER_AMOUNT_RUB = 100_000


class DailyCalculationJob:
    """Ежедневный расчёт индикаторов с EMA(13/26)."""

    def __init__(
        self,
        config: Config,
        repository: Repository,
        notifier: TelegramNotifier
    ):
        self.config = config
        self.repo = repository
        self.notifier = notifier

    async def run(self):
        """Запускает ежедневный расчёт."""
        logger.info("daily_calculation_started")
        
        now_msk = datetime.now(MSK)
        calc_date = now_msk.date()
        
        is_weekend = now_msk.weekday() >= 5
        if is_weekend:
            logger.info("running_on_weekend", message="Данные берутся за последние рабочие дни")
        
        report = {
            "date": now_msk.strftime("%Y-%m-%d %H:%M МСК"),
            "deposit": self.config.trading.deposit_rub,
            "risk_pct": self.config.trading.risk_per_trade_pct * 100,
            "dry_run": self.config.dry_run,
            "top_shares": [],
            "liquid_shares": [],
            "futures_si": None,
            "liquid_count": 0,
            "is_weekend": is_weekend,
        }

        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                # 1. Ликвидные акции
                liquid_shares = await self._get_liquid_shares(client)
                report["liquid_count"] = len(liquid_shares)
                report["liquid_shares"] = [s["ticker"] for s in liquid_shares]
                
                # 2. Анализ акций + сохранение в БД
                for share in liquid_shares[:15]:
                    result = await self._analyze_share(client, share, calc_date)
                    if result:
                        report["top_shares"].append(result)
                    await asyncio.sleep(0.3)
                
                # 3. Фьючерс Si
                report["futures_si"] = await self._analyze_futures_si(client, calc_date)
                
                # Сортировка: сначала UP тренд, потом по distance_to_ema
                report["top_shares"].sort(
                    key=lambda x: (
                        x.get("ema_trend") != "UP",  # UP первые
                        abs(x.get("distance_to_ema_13_pct", 100))  # Ближе к EMA
                    )
                )
                
                # 4. Обновляем кэш для кнопок Telegram
                all_for_cache = report["top_shares"].copy()
                if report["futures_si"]:
                    all_for_cache.append(report["futures_si"])
                update_shares_cache(all_for_cache)

        except Exception as e:
            logger.exception("daily_calculation_error")
            await self.notifier.send_error(str(e), "Ежедневный расчёт")
            return

        logger.info("sending_report", shares=len(report["top_shares"]))
        await self.notifier.send_daily_report(report)
        logger.info("daily_calculation_complete")

    async def _get_liquid_shares(self, client: TinkoffClient) -> List[Dict[str, Any]]:
        """Получает ликвидные акции."""
        logger.info("fetching_liquid_shares")
        
        all_shares = await client.get_shares()
        logger.info("total_shares", count=len(all_shares))
        
        shares_with_volume = []
        for i, share in enumerate(all_shares):
            if i % 20 == 0:
                logger.info("volume_check_progress", checked=i, total=len(all_shares))
            
            try:
                volume_data = await self._calculate_avg_volume(client, share["figi"])
                if volume_data and volume_data["avg_volume_rub"] >= self.config.liquidity.min_avg_volume_rub:
                    shares_with_volume.append({**share, **volume_data})
            except Exception as e:
                logger.error("volume_check_error", ticker=share["ticker"], error=str(e))
            
            await asyncio.sleep(0.15)
        
        shares_with_volume.sort(key=lambda x: x["avg_volume_rub"], reverse=True)
        logger.info("volume_check_complete", passed=len(shares_with_volume))
        
        liquid_shares = []
        check_count = min(len(shares_with_volume), self.config.liquidity.max_instruments * 2)
        
        for share in shares_with_volume[:check_count]:
            try:
                orderbook = await client.get_orderbook(share["figi"])
                if not orderbook:
                    continue
                if orderbook["spread_pct"] > self.config.liquidity.max_spread_pct:
                    continue
                
                liquid_shares.append({
                    **share,
                    "spread_pct": orderbook["spread_pct"],
                    "last_price": orderbook["mid_price"],
                })
                
                if len(liquid_shares) >= self.config.liquidity.max_instruments:
                    break
                    
            except Exception as e:
                logger.error("spread_check_error", ticker=share["ticker"], error=str(e))
            
            await asyncio.sleep(0.15)
        
        logger.info("liquidity_analysis_complete", count=len(liquid_shares))
        return liquid_shares

    async def _calculate_avg_volume(self, client: TinkoffClient, figi: str) -> Optional[Dict]:
        """Рассчитывает средний дневной объём."""
        days = self.config.liquidity.lookback_days
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=datetime.utcnow() - timedelta(days=days + 5),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        
        if not candles or len(candles) < days // 2:
            return None
        
        volumes_rub = [c["volume"] * c["close"] for c in candles[-days:]]
        if not volumes_rub:
            return None
        
        return {"avg_volume_rub": sum(volumes_rub) / len(volumes_rub)}

    async def _ensure_instrument(self, share: Dict) -> int:
        """Создаёт или получает инструмент в БД, возвращает ID."""
        instrument = await self.repo.get_instrument_by_figi(share["figi"])
        
        if not instrument:
            instrument = await self.repo.upsert_instrument({
                "figi": share["figi"],
                "ticker": share["ticker"],
                "name": share.get("name", share["ticker"]),
                "instrument_type": "share",
                "lot_size": share.get("lot", 1),
                "is_active": True,
                "is_liquid": True,
                "avg_volume_rub": share.get("avg_volume_rub"),
            })
        
        return instrument.id

    async def _analyze_share(
        self, 
        client: TinkoffClient, 
        share: Dict,
        calc_date: date
    ) -> Optional[Dict]:
        """Анализирует акцию и сохраняет в БД."""
        figi = share["figi"]
        ticker = share["ticker"]
        lot_size = share.get("lot", 1)
        
        logger.info("analyzing_share", ticker=ticker)
        
        # ЧАСОВЫЕ свечи за 40 дней (нужно 26 для EMA26)
        candles = await client.get_candles(
            figi=figi,
            from_dt=datetime.utcnow() - timedelta(days=40),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            logger.warning("no_candles", ticker=ticker)
            return None
        
        # Агрегация в дневные (10-19 МСК, пн-пт)
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 26:
            logger.warning("insufficient_daily", ticker=ticker, days=len(daily_df))
            return None
        
        # Индикаторы (включая EMA 13/26)
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        current_price = share.get("last_price") or indicators["close"]
        atr = indicators["atr"]
        bb_lower = indicators["bb_lower"]
        bb_middle = indicators["bb_middle"]
        
        # EMA данные
        ema_13 = indicators["ema_13"]
        ema_26 = indicators["ema_26"]
        ema_trend = indicators["ema_trend"]
        ema_diff_pct = indicators["ema_diff_pct"]
        distance_to_ema_13_pct = indicators["distance_to_ema_13_pct"]
        distance_to_ema_26_pct = indicators["distance_to_ema_26_pct"]
        
        # Расстояние до BB Lower
        distance_to_bb_pct = (current_price - bb_lower) / current_price * 100 if current_price > 0 else 0
        
        # === СОХРАНЕНИЕ В БД ===
        try:
            instrument_id = await self._ensure_instrument(share)
            
            await self.repo.save_indicator_daily(
                instrument_id=instrument_id,
                calc_date=calc_date,
                data={
                    "close": current_price,
                    "atr": atr,
                    "atr_pct": round(atr / current_price * 100, 2) if current_price > 0 else 0,
                    "bb_upper": indicators["bb_upper"],
                    "bb_middle": bb_middle,
                    "bb_lower": bb_lower,
                    "distance_to_bb_pct": round(distance_to_bb_pct, 2),
                    # EMA (13/26)
                    "ema_13": ema_13,
                    "ema_26": ema_26,
                    "ema_trend": ema_trend,
                    "ema_diff_pct": ema_diff_pct,
                    "ema_13_slope": indicators["ema_13_slope"],
                    "ema_26_slope": indicators["ema_26_slope"],
                    "distance_to_ema_13_pct": distance_to_ema_13_pct,
                    "distance_to_ema_26_pct": distance_to_ema_26_pct,
                    # Ликвидность
                    "volume_rub": share.get("avg_volume_rub"),
                    "spread_pct": share.get("spread_pct"),
                }
            )
            logger.info("indicators_saved_to_db", ticker=ticker, 
                       ema_trend=ema_trend, ema_13=ema_13, ema_26=ema_26)
        except Exception as e:
            logger.error("save_indicator_error", ticker=ticker, error=str(e))
        
        # === R:R 1:3 РАСЧЁТ ===
        entry_price = bb_lower
        take_profit_offset = 0.5 * atr
        take_price = entry_price + take_profit_offset
        stop_loss_offset = take_profit_offset / 3
        stop_price = entry_price - stop_loss_offset
        
        if entry_price > 0:
            position_size = int(ORDER_AMOUNT_RUB / entry_price)
        else:
            position_size = 0
        
        position_size = (position_size // lot_size) * lot_size
        position_value = position_size * entry_price
        potential_loss = position_size * stop_loss_offset
        potential_profit = position_size * take_profit_offset
        
        # Сигнал BUY если цена близко к BB Lower
        signal = "BUY" if distance_to_bb_pct <= self.config.bollinger.entry_threshold_pct else None
        
        logger.info("share_analyzed", 
                   ticker=ticker, 
                   price=round(current_price, 2),
                   ema_13=round(ema_13, 2),
                   ema_26=round(ema_26, 2),
                   ema_trend=ema_trend,
                   signal=signal)
        
        return {
            "ticker": ticker,
            "figi": figi,
            "lot_size": lot_size,
            "price": current_price,
            "atr": atr,
            "atr_pct": round(atr / current_price * 100, 2) if current_price > 0 else 0,
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "bb_upper": indicators["bb_upper"],
            # EMA (13/26)
            "ema_13": ema_13,
            "ema_26": ema_26,
            "ema_trend": ema_trend,
            "ema_diff_pct": ema_diff_pct,
            "ema_13_slope": indicators["ema_13_slope"],
            "ema_26_slope": indicators["ema_26_slope"],
            "distance_to_ema_13_pct": distance_to_ema_13_pct,
            "distance_to_ema_26_pct": distance_to_ema_26_pct,
            # R:R 1:3 параметры
            "entry_price": round(entry_price, 2),
            "take_price": round(take_price, 2),
            "stop_price": round(stop_price, 2),
            "stop_offset": round(stop_loss_offset, 2),
            "take_offset": round(take_profit_offset, 2),
            # Позиция
            "position_size": position_size,
            "position_value": round(position_value, 0),
            "potential_loss": round(potential_loss, 0),
            "potential_profit": round(potential_profit, 0),
            "distance_to_bb_pct": round(distance_to_bb_pct, 2),
            "signal": signal,
        }

    async def _analyze_futures_si(
        self, 
        client: TinkoffClient,
        calc_date: date
    ) -> Optional[Dict]:
        """Анализирует фьючерс Si и сохраняет в БД."""
        logger.info("analyzing_futures_si")
        
        si_future = await client.get_futures_by_ticker("Si")
        if not si_future:
            return None
        
        candles = await client.get_candles(
            figi=si_future["figi"],
            from_dt=datetime.utcnow() - timedelta(days=40),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            return None
        
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 26:
            return None
        
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        price = await client.get_last_price(si_future["figi"]) or indicators["close"]
        atr = indicators["atr"]
        bb_lower = indicators["bb_lower"]
        
        # EMA данные
        ema_13 = indicators["ema_13"]
        ema_26 = indicators["ema_26"]
        ema_trend = indicators["ema_trend"]
        
        # === СОХРАНЕНИЕ В БД ===
        try:
            instrument = await self.repo.get_instrument_by_figi(si_future["figi"])
            if not instrument:
                instrument = await self.repo.upsert_instrument({
                    "figi": si_future["figi"],
                    "ticker": si_future["ticker"],
                    "name": si_future.get("name", si_future["ticker"]),
                    "instrument_type": "future",
                    "lot_size": si_future.get("lot", 1),
                    "basic_asset": si_future.get("basic_asset"),
                    "expiration_date": si_future.get("expiration"),
                    "is_active": True,
                })
            
            await self.repo.save_indicator_daily(
                instrument_id=instrument.id,
                calc_date=calc_date,
                data={
                    "close": price,
                    "atr": atr,
                    "atr_pct": round(atr / price * 100, 2) if price > 0 else 0,
                    "bb_upper": indicators["bb_upper"],
                    "bb_middle": indicators["bb_middle"],
                    "bb_lower": bb_lower,
                    "ema_13": ema_13,
                    "ema_26": ema_26,
                    "ema_trend": ema_trend,
                    "ema_diff_pct": indicators["ema_diff_pct"],
                    "ema_13_slope": indicators["ema_13_slope"],
                    "ema_26_slope": indicators["ema_26_slope"],
                    "distance_to_ema_13_pct": indicators["distance_to_ema_13_pct"],
                    "distance_to_ema_26_pct": indicators["distance_to_ema_26_pct"],
                }
            )
            logger.info("si_indicators_saved", ticker=si_future["ticker"], ema_trend=ema_trend)
        except Exception as e:
            logger.error("save_si_indicator_error", error=str(e))
        
        # R:R 1:3 для Si
        entry_price = bb_lower
        take_profit_offset = 0.5 * atr
        take_price = entry_price + take_profit_offset
        stop_loss_offset = take_profit_offset / 3
        stop_price = entry_price - stop_loss_offset
        
        lot_size = si_future.get("lot", 1)
        position_size = int(ORDER_AMOUNT_RUB / entry_price) if entry_price > 0 else 0
        position_size = (position_size // lot_size) * lot_size
        position_value = position_size * entry_price
        potential_loss = position_size * stop_loss_offset
        potential_profit = position_size * take_profit_offset
        
        logger.info("si_analyzed", ticker=si_future["ticker"], price=price,
                   ema_13=round(ema_13, 0), ema_26=round(ema_26, 0), ema_trend=ema_trend)
        
        return {
            "ticker": si_future["ticker"],
            "figi": si_future["figi"],
            "lot_size": lot_size,
            "price": round(price, 0),
            "atr": round(atr, 0),
            "atr_pct": round(atr / price * 100, 2) if price > 0 else 0,
            "bb_lower": round(bb_lower, 0),
            "bb_middle": round(indicators["bb_middle"], 0),
            "bb_upper": round(indicators["bb_upper"], 0),
            # EMA (13/26)
            "ema_13": round(ema_13, 0),
            "ema_26": round(ema_26, 0),
            "ema_trend": ema_trend,
            "ema_diff_pct": indicators["ema_diff_pct"],
            "distance_to_ema_13_pct": indicators["distance_to_ema_13_pct"],
            "distance_to_ema_26_pct": indicators["distance_to_ema_26_pct"],
            # R:R параметры
            "entry_price": round(entry_price, 0),
            "take_price": round(take_price, 0),
            "stop_price": round(stop_price, 0),
            "stop_offset": round(stop_loss_offset, 0),
            "take_offset": round(take_profit_offset, 0),
            # Позиция
            "position_size": position_size,
            "position_value": round(position_value, 0),
            "potential_loss": round(potential_loss, 0),
            "potential_profit": round(potential_profit, 0),
            "expiration": si_future["expiration"].strftime("%Y-%m-%d"),
        }
