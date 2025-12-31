"""
Планируемые задачи.

DailyCalculationJob: ежедневный расчёт индикаторов в 06:30 МСК
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
from db.repository import Repository
from indicators.daily_aggregator import aggregate_hourly_to_daily, calculate_indicators

logger = structlog.get_logger()
MSK = pytz.timezone("Europe/Moscow")


class DailyCalculationJob:
    """Ежедневный расчёт индикаторов."""

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
        
        if now_msk.weekday() >= 5:
            logger.info("skipping_weekend")
            await self.notifier.send_message("📅 Выходной день, расчёт пропущен")
            return
        
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
                # 1. Ликвидные акции
                liquid_shares = await self._get_liquid_shares(client)
                report["liquid_count"] = len(liquid_shares)
                report["liquid_shares"] = [s["ticker"] for s in liquid_shares]
                
                # 2. Анализ акций
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

        except Exception as e:
            logger.exception("daily_calculation_error")
            await self.notifier.send_error(str(e), "Ежедневный расчёт")
            return

        logger.info("sending_report", shares=len(report["top_shares"]))
        await self.notifier.send_daily_report(report)
        logger.info("daily_calculation_complete")

    async def _get_liquid_shares(self, client: TinkoffClient) -> List[Dict[str, Any]]:
        """Получает ликвидные акции (проверяем ВСЕ, сортируем по объёму)."""
        logger.info("fetching_liquid_shares")
        
        all_shares = await client.get_shares()
        logger.info("total_shares", count=len(all_shares))
        
        # Шаг 1: Собираем объёмы для ВСЕХ акций
        shares_with_volume = []
        for i, share in enumerate(all_shares):
            if i % 20 == 0:
                logger.info("volume_check_progress", checked=i, total=len(all_shares))
            
            try:
                volume_data = await self._calculate_avg_volume(client, share["figi"])
                if volume_data and volume_data["avg_volume_rub"] >= self.config.liquidity.min_avg_volume_rub:
                    shares_with_volume.append({**share, **volume_data})
                    logger.debug("volume_passed", ticker=share["ticker"], 
                               volume_mln=round(volume_data["avg_volume_rub"] / 1_000_000, 1))
            except Exception as e:
                logger.error("volume_check_error", ticker=share["ticker"], error=str(e))
            
            await asyncio.sleep(0.15)
        
        # Сортируем по объёму (самые ликвидные сверху)
        shares_with_volume.sort(key=lambda x: x["avg_volume_rub"], reverse=True)
        logger.info("volume_check_complete", passed=len(shares_with_volume))
        
        # Шаг 2: Проверяем спред только для топ-N по объёму
        liquid_shares = []
        check_count = min(len(shares_with_volume), self.config.liquidity.max_instruments * 2)
        
        for share in shares_with_volume[:check_count]:
            try:
                orderbook = await client.get_orderbook(share["figi"])
                if not orderbook:
                    continue
                if orderbook["spread_pct"] > self.config.liquidity.max_spread_pct:
                    logger.debug("spread_too_high", ticker=share["ticker"], spread=orderbook["spread_pct"])
                    continue
                
                liquid_shares.append({
                    **share,
                    "spread_pct": orderbook["spread_pct"],
                    "last_price": orderbook["mid_price"],
                })
                
                logger.info("liquid_share_found", ticker=share["ticker"],
                           volume_mln=round(share["avg_volume_rub"] / 1_000_000, 1),
                           spread=round(orderbook["spread_pct"], 3))
                
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

    async def _analyze_share(self, client: TinkoffClient, share: Dict) -> Optional[Dict]:
        """Анализирует акцию."""
        figi = share["figi"]
        ticker = share["ticker"]
        lot_size = share.get("lot", 1)
        
        logger.info("analyzing_share", ticker=ticker)
        
        # ЧАСОВЫЕ свечи за 35 дней
        candles = await client.get_candles(
            figi=figi,
            from_dt=datetime.utcnow() - timedelta(days=35),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            logger.warning("no_candles", ticker=ticker)
            return None
        
        # Агрегация в дневные (10-19 МСК)
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 20:
            logger.warning("insufficient_daily", ticker=ticker, days=len(daily_df))
            return None
        
        # Индикаторы
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        current_price = share.get("last_price") or indicators["close"]
        atr = indicators["atr"]
        bb_lower = indicators["bb_lower"]
        
        # Расстояние до BB
        distance_to_bb_pct = (current_price - bb_lower) / current_price * 100 if current_price > 0 else 0
        
        # === РАСЧЁТ ПОЗИЦИИ С ОГРАНИЧЕНИЯМИ ===
        deposit = self.config.trading.deposit_rub
        risk_per_trade = self.config.trading.risk_per_trade_pct
        max_position_pct = self.config.risk.max_position_pct
        stop_loss_atr = self.config.risk.stop_loss_atr
        
        # Стоп-лосс в рублях на 1 акцию
        stop_rub = atr * stop_loss_atr
        
        # Максимальный убыток на сделку
        max_loss = deposit * risk_per_trade
        
        # Размер по риску: сколько акций можно купить при данном стопе
        if stop_rub > 0.001:
            position_by_risk = int(max_loss / stop_rub)
        else:
            position_by_risk = 0
        
        # Размер по капиталу: максимум 25% депозита
        max_position_value = deposit * max_position_pct
        if current_price > 0:
            position_by_capital = int(max_position_value / current_price)
        else:
            position_by_capital = 0
        
        # Берём МИНИМУМ из двух ограничений
        position_size = min(position_by_risk, position_by_capital)
        
        # Округляем до лотов
        position_size = (position_size // lot_size) * lot_size
        
        # Итоговая стоимость позиции
        position_value = position_size * current_price
        
        # Фактический риск
        actual_loss = position_size * stop_rub
        
        # Сигнал
        signal = "BUY" if distance_to_bb_pct <= self.config.bollinger.entry_threshold_pct else None
        
        logger.info("share_analyzed", 
                   ticker=ticker, 
                   price=round(current_price, 2),
                   atr=round(atr, 2), 
                   bb_lower=round(bb_lower, 2), 
                   distance=round(distance_to_bb_pct, 2), 
                   position=position_size,
                   position_value=round(position_value, 0),
                   signal=signal)
        
        return {
            "ticker": ticker,
            "price": current_price,
            "atr": atr,
            "atr_pct": round(atr / current_price * 100, 2) if current_price > 0 else 0,
            "bb_lower": bb_lower,
            "bb_upper": indicators["bb_upper"],
            "position_size": position_size,
            "position_value": round(position_value, 0),
            "stop_rub": stop_rub,
            "max_loss": round(actual_loss, 0),
            "distance_to_bb_pct": round(distance_to_bb_pct, 2),
            "signal": signal,
        }

    async def _analyze_futures_si(self, client: TinkoffClient) -> Optional[Dict]:
        """Анализирует фьючерс Si."""
        logger.info("analyzing_futures_si")
        
        si_future = await client.get_futures_by_ticker("Si")
        if not si_future:
            return None
        
        # ЧАСОВЫЕ свечи
        candles = await client.get_candles(
            figi=si_future["figi"],
            from_dt=datetime.utcnow() - timedelta(days=35),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            return None
        
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < 20:
            return None
        
        indicators = calculate_indicators(daily_df)
        if not indicators:
            return None
        
        price = await client.get_last_price(si_future["figi"]) or indicators["close"]
        
        logger.info("si_analyzed", ticker=si_future["ticker"], price=price,
                   atr=indicators["atr"], bb_lower=indicators["bb_lower"])
        
        return {
            "ticker": si_future["ticker"],
            "price": round(price, 0),
            "atr": round(indicators["atr"], 0),
            "bb_lower": round(indicators["bb_lower"], 0),
            "expiration": si_future["expiration"].strftime("%Y-%m-%d"),
        }