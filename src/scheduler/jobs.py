"""
Планируемые задачи.

DailyCalculationJob: ежедневный расчёт индикаторов в 06:30 МСК

Диагностика:
- Логирует причины отсутствия ликвидных акций
- Расширенный lookback для праздников
- Информативные сообщения в Telegram
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
    """Ежедневный расчёт индикаторов с диагностикой."""

    def __init__(
        self,
        config: Config,
        repository: Repository,
        notifier: TelegramNotifier
    ):
        self.config = config
        self.repo = repository
        self.notifier = notifier
        
        # Статистика для диагностики
        self._diagnostics = {
            "total_shares": 0,
            "volume_passed": 0,
            "volume_failed": 0,
            "spread_passed": 0,
            "spread_failed": 0,
            "indicators_ok": 0,
            "indicators_failed": 0,
            "insufficient_days": 0,
            "no_candles": 0,
            "errors": [],
        }

    def _reset_diagnostics(self):
        """Сбрасывает статистику."""
        self._diagnostics = {
            "total_shares": 0,
            "volume_passed": 0,
            "volume_failed": 0,
            "spread_passed": 0,
            "spread_failed": 0,
            "indicators_ok": 0,
            "indicators_failed": 0,
            "insufficient_days": 0,
            "no_candles": 0,
            "errors": [],
        }

    async def run(self):
        """Запускает ежедневный расчёт."""
        logger.info("daily_calculation_started")
        self._reset_diagnostics()
        
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
            "diagnostics": None,  # Добавим диагностику
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
                        self._diagnostics["indicators_ok"] += 1
                    else:
                        self._diagnostics["indicators_failed"] += 1
                    await asyncio.sleep(0.3)
                
                # 3. Фьючерс Si
                report["futures_si"] = await self._analyze_futures_si(client, calc_date)
                
                # Сортировка: сначала UP тренд, потом по distance_to_ema
                report["top_shares"].sort(
                    key=lambda x: (
                        x.get("ema_trend") != "UP",
                        abs(x.get("distance_to_ema_13_pct", 100))
                    )
                )
                
                # 4. Обновляем кэш для кнопок Telegram
                all_for_cache = report["top_shares"].copy()
                if report["futures_si"]:
                    all_for_cache.append(report["futures_si"])
                update_shares_cache(all_for_cache)
                
                # 5. Добавляем диагностику в отчёт
                report["diagnostics"] = self._diagnostics.copy()

        except Exception as e:
            logger.exception("daily_calculation_error")
            self._diagnostics["errors"].append(str(e))
            await self.notifier.send_error(str(e), "Ежедневный расчёт")
            return

        logger.info("sending_report", 
                   shares=len(report["top_shares"]),
                   diagnostics=self._diagnostics)
        
        await self._send_report_with_diagnostics(report)
        logger.info("daily_calculation_complete")

    async def _send_report_with_diagnostics(self, report: Dict[str, Any]):
        """Отправляет отчёт с диагностикой."""
        diag = report.get("diagnostics", {})
        
        # Основной отчёт
        await self.notifier.send_daily_report(report)
        
        # Если 0 акций — отправляем диагностику
        if report["liquid_count"] == 0:
            reasons = []
            
            if diag.get("total_shares", 0) == 0:
                reasons.append("• API не вернул акции")
            
            if diag.get("volume_failed", 0) > 0:
                reasons.append(
                    f"• Объём ниже порога: {diag['volume_failed']} акций\n"
                    f"  (порог: {self.config.liquidity.min_avg_volume_rub/1_000_000:.0f} млн ₽/день)"
                )
            
            if diag.get("spread_failed", 0) > 0:
                reasons.append(
                    f"• Спред выше порога: {diag['spread_failed']} акций\n"
                    f"  (порог: {self.config.liquidity.max_spread_pct}%)"
                )
            
            if diag.get("insufficient_days", 0) > 0:
                reasons.append(
                    f"• Недостаточно торговых дней: {diag['insufficient_days']} акций\n"
                    f"  (нужно минимум {self.config.liquidity.min_trading_days} дней для EMA26)"
                )
            
            if diag.get("no_candles", 0) > 0:
                reasons.append(f"• Нет свечей: {diag['no_candles']} акций")
            
            if diag.get("errors"):
                reasons.append(f"• Ошибки: {len(diag['errors'])}")
            
            if not reasons:
                reasons.append("• Причина не определена")
            
            # Совет
            advice = []
            if diag.get("insufficient_days", 0) > 0:
                advice.append(
                    "💡 Возможно, это связано с праздниками (мало торговых дней).\n"
                    "Бот использует расширенный lookback (90 дней), но после длинных "
                    "праздников может потребоваться несколько рабочих дней для накопления данных."
                )
            
            advice_text = "\n\n".join(advice) if advice else ""
            
            await self.notifier.send_message(
                f"🔍 <b>Диагностика: почему 0 ликвидных акций</b>\n\n"
                f"📊 <b>Статистика:</b>\n"
                f"• Всего акций в API: {diag.get('total_shares', 0)}\n"
                f"• Прошли фильтр объёма: {diag.get('volume_passed', 0)}\n"
                f"• Прошли фильтр спреда: {diag.get('spread_passed', 0)}\n"
                f"• Индикаторы рассчитаны: {diag.get('indicators_ok', 0)}\n\n"
                f"❌ <b>Причины отсева:</b>\n"
                f"{chr(10).join(reasons)}\n\n"
                f"{advice_text}"
            )

    async def _get_liquid_shares(self, client: TinkoffClient) -> List[Dict[str, Any]]:
        """Получает ликвидные акции с диагностикой."""
        logger.info("fetching_liquid_shares")
        
        all_shares = await client.get_shares()
        self._diagnostics["total_shares"] = len(all_shares)
        logger.info("total_shares", count=len(all_shares))
        
        shares_with_volume = []
        for i, share in enumerate(all_shares):
            if i % 20 == 0:
                logger.info("volume_check_progress", checked=i, total=len(all_shares))
            
            try:
                volume_data = await self._calculate_avg_volume(client, share["figi"])
                if volume_data:
                    if volume_data["avg_volume_rub"] >= self.config.liquidity.min_avg_volume_rub:
                        shares_with_volume.append({**share, **volume_data})
                        self._diagnostics["volume_passed"] += 1
                    else:
                        self._diagnostics["volume_failed"] += 1
                else:
                    self._diagnostics["no_candles"] += 1
            except Exception as e:
                logger.error("volume_check_error", ticker=share["ticker"], error=str(e))
                self._diagnostics["errors"].append(f"{share['ticker']}: {str(e)[:50]}")
            
            await asyncio.sleep(0.15)
        
        shares_with_volume.sort(key=lambda x: x["avg_volume_rub"], reverse=True)
        logger.info("volume_check_complete", 
                   passed=self._diagnostics["volume_passed"],
                   failed=self._diagnostics["volume_failed"])
        
        # Проверка спреда
        liquid_shares = []
        check_count = min(len(shares_with_volume), self.config.liquidity.max_instruments * 2)
        
        for share in shares_with_volume[:check_count]:
            try:
                # Получаем цену (спред не проверяем)
                last_price = await client.get_last_price(share["figi"])
                if not last_price:
                    continue
                
                liquid_shares.append({
                    **share,
                    "last_price": last_price,
                })
                self._diagnostics["spread_passed"] += 1
                
                if len(liquid_shares) >= self.config.liquidity.max_instruments:
                    break
                    
            except Exception as e:
                logger.error("spread_check_error", ticker=share["ticker"], error=str(e))
                self._diagnostics["spread_failed"] += 1
            
            await asyncio.sleep(0.15)
        
        logger.info("liquidity_analysis_complete", count=len(liquid_shares))
        return liquid_shares

    async def _calculate_avg_volume(self, client: TinkoffClient, figi: str) -> Optional[Dict]:
        """
        Рассчитывает средний дневной объём.
        
        Использует расширенный lookback (90 дней) для праздников.
        """
        # Используем расширенный lookback для учёта праздников
        extended_days = self.config.liquidity.extended_lookback_days
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=datetime.utcnow() - timedelta(days=extended_days),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_DAY
        )
        
        if not candles:
            return None
        
        # Берём последние N дней
        lookback = self.config.liquidity.lookback_days
        recent = candles[-lookback:] if len(candles) >= lookback else candles
        
        if len(recent) < lookback // 2:
            return None
        
        volumes_rub = [c["volume"] * c["close"] for c in recent]
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
        
        # Расширенный lookback для праздников
        extended_days = self.config.liquidity.extended_lookback_days
        min_trading_days = self.config.liquidity.min_trading_days
        
        candles = await client.get_candles(
            figi=figi,
            from_dt=datetime.utcnow() - timedelta(days=extended_days),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            logger.warning("no_candles", ticker=ticker)
            self._diagnostics["no_candles"] += 1
            return None
        
        # Агрегация в дневные (10-19 МСК, пн-пт)
        daily_df = aggregate_hourly_to_daily(candles)
        
        if daily_df.empty:
            logger.warning("empty_daily_df", ticker=ticker)
            self._diagnostics["no_candles"] += 1
            return None
        
        if len(daily_df) < min_trading_days:
            logger.warning("insufficient_daily", 
                          ticker=ticker, 
                          days=len(daily_df),
                          required=min_trading_days)
            self._diagnostics["insufficient_days"] += 1
            return None
        
        # Индикаторы (включая EMA 13/26)
        indicators = calculate_indicators(daily_df)
        if not indicators:
            self._diagnostics["indicators_failed"] += 1
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
                   trading_days=len(daily_df),
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
            # Диагностика
            "trading_days": len(daily_df),
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
        
        extended_days = self.config.liquidity.extended_lookback_days
        min_trading_days = self.config.liquidity.min_trading_days
        
        candles = await client.get_candles(
            figi=si_future["figi"],
            from_dt=datetime.utcnow() - timedelta(days=extended_days),
            to_dt=datetime.utcnow(),
            interval=CandleInterval.CANDLE_INTERVAL_HOUR
        )
        
        if not candles:
            return None
        
        daily_df = aggregate_hourly_to_daily(candles)
        if daily_df.empty or len(daily_df) < min_trading_days:
            logger.warning("si_insufficient_days", 
                          days=len(daily_df) if not daily_df.empty else 0,
                          required=min_trading_days)
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
                   ema_13=round(ema_13, 0), ema_26=round(ema_26, 0), ema_trend=ema_trend,
                   trading_days=len(daily_df))
        
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
            # Диагностика
            "trading_days": len(daily_df),
        }