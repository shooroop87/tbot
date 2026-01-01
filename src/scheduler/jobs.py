"""
Планируемые задачи.

DailyCalculationJob: ежедневный расчёт индикаторов в 06:30 МСК

Принципы:
- День запуска НИКОГДА не включается в расчёт (ATR, BB, ликвидность)
- Всегда анализируем "вчера и раньше" 
- Стакан не используем — ликвидность по историческому объёму
- Расчёт можно запускать в любой день (праздники, выходные)
"""
import asyncio
from datetime import datetime, timedelta
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
    """Ежедневный расчёт индикаторов с R:R 1:3."""

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
        
        report = {
            "date": now_msk.strftime("%Y-%m-%d %H:%M МСК"),
            "deposit": self.config.trading.deposit_rub,
            "risk_pct": self.config.trading.risk_per_trade_pct * 100,
            "dry_run": self.config.dry_run,
            "top_shares": [],
            "liquid_shares": [],
            "futures_si": None,
            "liquid_count": 0,
        }

        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                # 1. Ликвидные акции (по историческому объёму, без стакана)
                liquid_shares = await self._get_liquid_shares(client)
                report["liquid_count"] = len(liquid_shares)
                report["liquid_shares"] = [s["ticker"] for s in liquid_shares]
                
                # 2. Анализ акций с R:R 1:3
                for share in liquid_shares[:15]:
                    result = await self._analyze_share(client, share)
                    if result:
                        report["top_shares"].append(result)
                    await asyncio.sleep(0.3)
                
                # 3. Фьючерс Si
                report["futures_si"] = await self._analyze_futures_si(client)
                
                # Сортировка: сначала BUY, потом по distance
                report["top_shares"].sort(
                    key=lambda x: (x.get("signal") != "BUY", x.get("distance_to_bb_pct", 100))
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
        """
        Получает ликвидные акции по историческому объёму.
        
        НЕ использует стакан — работает в любой день.
        """
        logger.info("fetching_liquid_shares")
        
        all_shares = await client.get_shares()
        logger.info("total_shares", count=len(all_shares))
        
        # Собираем объёмы и последнюю цену для ВСЕХ акций
        shares_with_volume = []
        for i, share in enumerate(all_shares):
            if i % 20 == 0:
                logger.info("volume_check_progress", checked=i, total=len(all_shares))
            
            try:
                volume_data = await self._calculate_avg_volume_and_price(client, share["figi"])
                if volume_data and volume_data["avg_volume_rub"] >= self.config.liquidity.min_avg_volume_rub:
                    shares_with_volume.append({**share, **volume_data})
                    logger.debug("volume_passed", ticker=share["ticker"], 
                               volume_mln=round(volume_data["avg_volume_rub"] / 1_000_000, 1))
            except Exception as e:
                logger.error("volume_check_error", ticker=share["ticker"], error=str(e))
            
            await asyncio.sleep(0.15)
        
        # Сортируем по объёму (самые ликвидные сверху)
        shares_with_volume.sort(key=lambda x: x["avg_volume_rub"], reverse=True)
        
        # Берём топ-N
        liquid_shares = shares_with_volume[:self.config.liquidity.max_instruments]
        
        for share in liquid_shares:
            logger.info("liquid_share_found", 
                       ticker=share["ticker"],
                       volume_mln=round(share["avg_volume_rub"] / 1_000_000, 1),
                       last_price=share.get("last_price", 0))
        
        logger.info("liquidity_analysis_complete", count=len(liquid_shares))
        return liquid_shares

    async def _calculate_avg_volume_and_price(self, client: TinkoffClient, figi: str) -> Optional[Dict]:
        """
        Рассчитывает средний дневной объём и последнюю цену.
        
        ВАЖНО: сегодня НЕ включается в расчёт.
        Берём данные за [today - lookback_days - 5, yesterday].
        """
        days = self.config.liquidity.lookback_days
        
        # Конец = вчера 23:59 (сегодня исключаем)
        now = datetime.utcnow()
        to_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)  # начало сегодня = конец вчера
        from_dt = to_dt - timedelta(days=days + 5)
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=from_dt,
            to_dt=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        
        if not candles or len(candles) < days // 2:
            return None
        
        # Последние N дней (уже без сегодня)
        recent_candles = candles[-days:]
        volumes_rub = [c["volume"] * c["close"] for c in recent_candles]
        
        if not volumes_rub:
            return None
        
        # Последняя цена = close последней свечи
        last_price = recent_candles[-1]["close"]
        
        return {
            "avg_volume_rub": sum(volumes_rub) / len(volumes_rub),
            "last_price": last_price,
        }

    async def _analyze_share(self, client: TinkoffClient, share: Dict) -> Optional[Dict]:
        """
        Анализирует акцию с R:R 1:3.
        
        ВАЖНО: сегодня НЕ включается в расчёт ATR и BB.
        """
        figi = share["figi"]
        ticker = share["ticker"]
        lot_size = share.get("lot", 1)
        
        logger.info("analyzing_share", ticker=ticker)
        
        # ЧАСОВЫЕ свечи за 35 дней ДО СЕГОДНЯ
        now = datetime.utcnow()
        to_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)  # начало сегодня
        from_dt = to_dt - timedelta(days=35)
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=from_dt,
            to_dt=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            logger.warning("no_candles", ticker=ticker)
            return None
        
        # Агрегация в дневные (10-19 МСК, пн-пт)
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 20:
            logger.warning("insufficient_daily", ticker=ticker, days=len(daily_df))
            return None
        
        # Индикаторы
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        # Текущая цена = из share (уже получили в _get_liquid_shares)
        current_price = share.get("last_price") or indicators["close"]
        atr = indicators["atr"]
        bb_lower = indicators["bb_lower"]
        bb_middle = indicators["bb_middle"]
        
        # Расстояние до BB Lower
        distance_to_bb_pct = (current_price - bb_lower) / current_price * 100 if current_price > 0 else 0
        
        # === R:R 1:3 РАСЧЁТ ===
        entry_price = bb_lower
        take_profit_offset = 0.5 * atr
        take_price = entry_price + take_profit_offset
        reward = take_profit_offset
        stop_loss_offset = reward / 3
        stop_price = entry_price - stop_loss_offset
        
        # === РАСЧЁТ ПОЗИЦИИ ===
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
                   entry=round(entry_price, 2),
                   take=round(take_price, 2),
                   stop=round(stop_price, 2),
                   atr=round(atr, 2), 
                   bb_lower=round(bb_lower, 2), 
                   distance=round(distance_to_bb_pct, 2), 
                   position=position_size,
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
            "entry_price": round(entry_price, 2),
            "take_price": round(take_price, 2),
            "stop_price": round(stop_price, 2),
            "stop_offset": round(stop_loss_offset, 2),
            "take_offset": round(take_profit_offset, 2),
            "position_size": position_size,
            "position_value": round(position_value, 0),
            "potential_loss": round(potential_loss, 0),
            "potential_profit": round(potential_profit, 0),
            "distance_to_bb_pct": round(distance_to_bb_pct, 2),
            "signal": signal,
        }

    async def _analyze_futures_si(self, client: TinkoffClient) -> Optional[Dict]:
        """
        Анализирует фьючерс Si с R:R 1:3.
        
        ВАЖНО: сегодня НЕ включается в расчёт.
        """
        logger.info("analyzing_futures_si")
        
        si_future = await client.get_futures_by_ticker("Si")
        if not si_future:
            return None
        
        # ЧАСОВЫЕ свечи ДО СЕГОДНЯ
        now = datetime.utcnow()
        to_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        from_dt = to_dt - timedelta(days=35)
        
        candles = await client.get_candles(
            figi=si_future["figi"],
            from_dt=from_dt,
            to_dt=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            return None
        
        # Агрегация в дневные (10-19 МСК, пн-пт)
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 20:
            return None
        
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        # Цена = последняя свеча (вчера close)
        price = indicators["close"]
        atr = indicators["atr"]
        bb_lower = indicators["bb_lower"]
        
        # === R:R 1:3 для Si ===
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
                   atr=round(atr, 0), bb_lower=round(bb_lower, 0),
                   entry=round(entry_price, 0))
        
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
            "entry_price": round(entry_price, 0),
            "take_price": round(take_price, 0),
            "stop_price": round(stop_price, 0),
            "stop_offset": round(stop_loss_offset, 0),
            "take_offset": round(take_profit_offset, 0),
            "position_size": position_size,
            "position_value": round(position_value, 0),
            "potential_loss": round(potential_loss, 0),
            "potential_profit": round(potential_profit, 0),
            "expiration": si_future["expiration"].strftime("%Y-%m-%d"),
        }