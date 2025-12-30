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
from strategy.bollinger_bounce import BollingerBounceStrategy

logger = structlog.get_logger()
MSK = pytz.timezone("Europe/Moscow")


class DailyCalculationJob:
    """
    Ежедневный расчёт индикаторов.
    
    Выполняет:
    1. Получение ликвидных акций
    2. Загрузка свечей
    3. Расчёт ATR, Bollinger
    4. Генерация сигналов
    5. Сохранение в БД
    6. Отправка отчёта в Telegram
    """

    def __init__(
        self,
        config: Config,
        repository: Repository,
        notifier: TelegramNotifier
    ):
        self.config = config
        self.repo = repository
        self.notifier = notifier
        
        # Инициализация стратегии
        self.strategy = BollingerBounceStrategy({
            "bollinger_period": config.bollinger.period,
            "bollinger_std": config.bollinger.std_multiplier,
            "atr_period": config.atr.period,
            "entry_threshold_pct": config.bollinger.entry_threshold_pct,
            "stop_loss_atr": config.risk.stop_loss_atr,
            "take_profit_atr": config.risk.take_profit_atr,
            "deposit": config.trading.deposit_rub,
            "risk_per_trade": config.trading.risk_per_trade_pct,
            "max_position_pct": config.risk.max_position_pct,
            "trading_start_hour": int(config.trading_hours.start.split(":")[0]),
            "trading_end_hour": int(config.trading_hours.end.split(":")[0]),
        })

    async def run(self):
        """Запускает ежедневный расчёт."""
        logger.info("daily_calculation_started")
        
        now_msk = datetime.now(MSK)
        
        # Проверка: выходной?
        if now_msk.weekday() >= 5:
            logger.info("skipping_weekend", day=now_msk.strftime("%A"))
            await self.notifier.send_message("📅 Выходной день, расчёт пропущен")
            return
        
        report = {
            "date": now_msk.strftime("%Y-%m-%d %H:%M МСК"),
            "deposit": self.config.trading.deposit_rub,
            "risk_pct": self.config.trading.risk_per_trade_pct * 100,
            "dry_run": self.config.dry_run,
            "top_shares": [],
            "futures_si": None,
            "liquid_count": 0,
        }

        try:
            async with TinkoffClient(self.config.tinkoff) as client:
                # 1. Получаем ликвидные акции
                liquid_shares = await self._get_liquid_shares(client)
                report["liquid_count"] = len(liquid_shares)
                
                # 2. Анализируем каждую акцию
                for share in liquid_shares[:15]:  # Топ-15
                    result = await self._analyze_share(client, share)
                    if result:
                        report["top_shares"].append(result)
                        await asyncio.sleep(0.3)
                
                # 3. Фьючерс Si
                futures_si = await self._analyze_futures_si(client)
                if futures_si:
                    report["futures_si"] = futures_si
                
                # Сортируем: сначала с сигналом BUY
                report["top_shares"].sort(
                    key=lambda x: (x.get("signal") != "BUY", x.get("distance_to_bb_pct", 100))
                )

        except Exception as e:
            logger.exception("daily_calculation_error")
            await self.notifier.send_error(str(e), "Ежедневный расчёт")
            return

        # 4. Отправляем отчёт
        logger.info("sending_report", shares=len(report["top_shares"]))
        await self.notifier.send_daily_report(report)
        
        logger.info("daily_calculation_complete")

    async def _get_liquid_shares(self, client: TinkoffClient) -> List[Dict[str, Any]]:
        """Получает ликвидные акции."""
        logger.info("fetching_liquid_shares")
        
        all_shares = await client.get_shares()
        liquid_shares = []
        
        for share in all_shares[:self.config.liquidity.max_instruments]:
            figi = share["figi"]
            
            try:
                # Проверяем объём
                volume_data = await self._calculate_avg_volume(client, figi)
                if not volume_data:
                    continue
                    
                if volume_data["avg_volume_rub"] < self.config.liquidity.min_avg_volume_rub:
                    continue
                
                # Проверяем спред
                orderbook = await client.get_orderbook(figi)
                if not orderbook:
                    continue
                    
                if orderbook["spread_pct"] > self.config.liquidity.max_spread_pct:
                    continue
                
                # Сохраняем в БД
                await self.repo.upsert_instrument({
                    "figi": figi,
                    "ticker": share["ticker"],
                    "name": share["name"],
                    "instrument_type": "share",
                    "lot_size": share.get("lot", 1),
                    "avg_volume_rub": volume_data["avg_volume_rub"],
                    "spread_pct": orderbook["spread_pct"],
                    "is_liquid": True,
                })
                
                liquid_shares.append({
                    **share,
                    "avg_volume_rub": volume_data["avg_volume_rub"],
                    "spread_pct": orderbook["spread_pct"],
                    "last_price": orderbook["mid_price"],
                })
                
                logger.info(
                    "liquid_share_found",
                    ticker=share["ticker"],
                    volume_mln=round(volume_data["avg_volume_rub"] / 1_000_000, 1)
                )
                
                await asyncio.sleep(0.3)
                
            except Exception as e:
                logger.error("liquidity_check_error", ticker=share["ticker"], error=str(e))
        
        # Сортируем по объёму
        liquid_shares.sort(key=lambda x: x["avg_volume_rub"], reverse=True)
        
        logger.info("liquidity_analysis_complete", count=len(liquid_shares))
        return liquid_shares

    async def _calculate_avg_volume(
        self, 
        client: TinkoffClient, 
        figi: str
    ) -> Optional[Dict[str, Any]]:
        """Рассчитывает средний дневной объём."""
        days = self.config.liquidity.lookback_days
        
        to_dt = datetime.utcnow()
        from_dt = to_dt - timedelta(days=days + 5)
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=from_dt,
            to_dt=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        
        if not candles or len(candles) < days // 2:
            return None
        
        volumes_rub = [c["volume"] * c["close"] for c in candles[-days:]]
        
        if not volumes_rub:
            return None
        
        return {
            "avg_volume_rub": sum(volumes_rub) / len(volumes_rub),
            "days_analyzed": len(volumes_rub),
        }

    async def _analyze_share(
        self, 
        client: TinkoffClient, 
        share: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Анализирует акцию и возвращает данные для отчёта."""
        figi = share["figi"]
        ticker = share["ticker"]
        
        logger.info("analyzing_share", ticker=ticker)
        
        # Загружаем часовые свечи за 35 дней
        from_dt = datetime.utcnow() - timedelta(days=35)
        to_dt = datetime.utcnow()
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=from_dt,
            to_dt=to_dt,
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            logger.warning("no_candles", ticker=ticker)
            return None
        
        # Текущая цена
        current_price = share.get("last_price") or candles[-1]["close"]
        
        # Анализ стратегией
        analysis = self.strategy.analyze(
            ticker=ticker,
            figi=figi,
            candles=candles,
            current_price=current_price,
            lot_size=share.get("lot", 1),
        )
        
        if not analysis:
            return None
        
        # Сохраняем индикаторы в БД
        instrument = await self.repo.get_instrument_by_figi(figi)
        if instrument:
            await self.repo.save_daily_indicator({
                "instrument_id": instrument.id,
                "date": datetime.now(MSK).replace(tzinfo=None),
                "last_price": current_price,
                "atr": analysis.atr,
                "atr_pct": analysis.atr_pct,
                "bb_upper": analysis.bb_upper,
                "bb_middle": analysis.bb_middle,
                "bb_lower": analysis.bb_lower,
                "recommended_size": analysis.position_size,
                "stop_rub": analysis.stop_rub,
                "max_loss_rub": analysis.max_loss,
            })
            
            # Сохраняем сигнал если есть
            if analysis.signal:
                await self.repo.save_signal({
                    "instrument_id": instrument.id,
                    "signal_type": analysis.signal.type.value,
                    "signal_time": analysis.signal.timestamp,
                    "price": analysis.signal.price,
                    "target_price": analysis.signal.target_price,
                    "stop_price": analysis.signal.stop_price,
                    "position_size": analysis.signal.position_size,
                    "strategy": analysis.signal.strategy_name,
                    "reason": analysis.signal.reason,
                    "confidence": analysis.signal.confidence,
                })
        
        return {
            "ticker": ticker,
            "price": current_price,
            "atr": analysis.atr,
            "atr_pct": analysis.atr_pct,
            "bb_lower": analysis.bb_lower,
            "bb_upper": analysis.bb_upper,
            "position_size": analysis.position_size,
            "position_value": analysis.position_value,
            "stop_rub": analysis.stop_rub,
            "max_loss": analysis.max_loss,
            "distance_to_bb_pct": analysis.distance_to_bb_pct,
            "signal": analysis.signal.type.value if analysis.signal else None,
        }

    async def _analyze_futures_si(self, client: TinkoffClient) -> Optional[Dict[str, Any]]:
        """Анализирует фьючерс Si."""
        logger.info("analyzing_futures_si")
        
        si_future = await client.get_next_futures("USD")
        if not si_future:
            return None
        
        candles = await client.get_candles(
            figi=si_future["figi"],
            from_dt=datetime.utcnow() - timedelta(days=35),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            return None
        
        price = await client.get_last_price(si_future["figi"]) or candles[-1]["close"]
        
        analysis = self.strategy.analyze(
            ticker=si_future["ticker"],
            figi=si_future["figi"],
            candles=candles,
            current_price=price,
            lot_size=si_future.get("lot", 1),
        )
        
        if not analysis:
            return None
        
        return {
            "ticker": si_future["ticker"],
            "price": price,
            "atr": analysis.atr,
            "bb_lower": analysis.bb_lower,
            "expiration": si_future["expiration"].strftime("%Y-%m-%d"),
        }
