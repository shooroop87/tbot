"""
Отправка уведомлений в Telegram.
"""
from typing import Optional, List, Dict, Any

import aiohttp
import structlog

from config import TelegramConfig

logger = structlog.get_logger()


class TelegramNotifier:
    """Асинхронный клиент для отправки сообщений в Telegram."""

    def __init__(self, config: TelegramConfig):
        self.bot_token = config.bot_token
        self.chat_id = config.chat_id
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"

    async def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False
    ) -> bool:
        """Отправляет сообщение в Telegram."""
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (обрезано)"

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        logger.debug("telegram_sent", chars=len(text))
                        return True
                    else:
                        error = await response.text()
                        logger.error("telegram_error", status=response.status, error=error[:200])
                        return False
        except Exception as e:
            logger.error("telegram_exception", error=str(e))
            return False

    def _format_price(self, price: float) -> str:
        """Форматирует цену с учётом величины."""
        if price >= 1000:
            return f"{price:,.0f}"
        elif price >= 10:
            return f"{price:.1f}"
        else:
            return f"{price:.2f}"

    async def send_daily_report(self, report: Dict[str, Any]) -> bool:
        """Отправляет ежедневный отчёт."""
        lines = [
            "📊 <b>Ежедневный расчёт</b>",
            f"📅 {report['date']}",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"🔍 Ликвидных акций: <b>{report.get('liquid_count', 0)}</b>",
            f"💰 Депозит: {report.get('deposit', 0):,.0f} ₽",
            f"⚠️ Риск на сделку: {report.get('risk_pct', 1)}%",
        ]

        # Список ликвидных акций
        liquid_shares = report.get("liquid_shares", [])
        if liquid_shares:
            lines.append(f"📋 Ликвидные: {', '.join(liquid_shares)}")
        
        lines.append("")

        # Акции с индикаторами
        top_shares = report.get("top_shares", [])
        if top_shares:
            lines.append("<b>📈 ТОП акции:</b>")
            lines.append("")

            for share in top_shares[:10]:
                emoji = "🟢" if share.get("signal") == "BUY" else "⚪"
                price = share['price']
                bb_lower = share['bb_lower']
                stop_rub = share['stop_rub']
                atr = share['atr']
                
                lines.extend([
                    f"{emoji} <b>{share['ticker']}</b>",
                    f"   💵 Цена: {self._format_price(price)} ₽",
                    f"   📊 ATR: {self._format_price(atr)} ({share['atr_pct']:.1f}%)",
                    f"   📉 BB нижняя: {self._format_price(bb_lower)} ₽",
                    f"   📦 Позиция: {share['position_size']:,} шт ({share['position_value']:,.0f} ₽)",
                    f"   🛑 Стоп: {self._format_price(stop_rub)} ₽ | Убыток: {share['max_loss']:,.0f} ₽",
                    f"   📏 До BB: {share.get('distance_to_bb_pct', 0):.1f}%",
                    "",
                ])
        else:
            lines.append("<i>Нет акций с достаточными данными</i>")
            lines.append("")

        # Фьючерс Si
        futures_si = report.get("futures_si")
        if futures_si:
            lines.extend([
                "<b>💵 Фьючерс Si:</b>",
                f"   Тикер: {futures_si['ticker']}",
                f"   Цена: {futures_si['price']:,.0f}",
                f"   ATR: {futures_si['atr']:.0f}",
                f"   BB нижняя: {futures_si['bb_lower']:,.0f}",
                "",
            ])

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━",
            "⚠️ <i>Торговля несёт риск потери капитала</i>",
            f"🤖 dry_run: {report.get('dry_run', True)}",
        ])

        text = "\n".join(lines)
        return await self.send_message(text)

    async def send_signal(self, ticker: str, signal_type: str, price: float,
                          target: Optional[float] = None, stop: Optional[float] = None,
                          size: Optional[int] = None, reason: str = "") -> bool:
        """Отправляет торговый сигнал."""
        emoji_map = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "⚪"}
        emoji = emoji_map.get(signal_type, "⚪")

        lines = [
            f"{emoji} <b>{signal_type}</b> {ticker}",
            f"💰 Цена: {self._format_price(price)} ₽",
        ]

        if size:
            lines.append(f"📦 Объём: {size:,} шт")
        if target:
            lines.append(f"🎯 Цель: {self._format_price(target)} ₽")
        if stop:
            lines.append(f"🛑 Стоп: {self._format_price(stop)} ₽")
        if reason:
            lines.append(f"📝 {reason}")

        return await self.send_message("\n".join(lines))

    async def send_trade_result(self, ticker: str, entry_price: float, exit_price: float,
                                size: int, pnl_rub: float, pnl_pct: float, reason: str = "") -> bool:
        """Отправляет результат закрытой сделки."""
        emoji = "✅" if pnl_rub >= 0 else "❌"

        lines = [
            f"{emoji} <b>Сделка закрыта:</b> {ticker}",
            f"📥 Вход: {self._format_price(entry_price)} ₽",
            f"📤 Выход: {self._format_price(exit_price)} ₽",
            f"📦 Объём: {size:,} шт",
            f"💰 P&L: {pnl_rub:+,.0f} ₽ ({pnl_pct:+.2f}%)",
        ]
        if reason:
            lines.append(f"📝 {reason}")

        return await self.send_message("\n".join(lines))

    async def send_error(self, error_msg: str, context: str = "") -> bool:
        """Отправляет сообщение об ошибке."""
        lines = ["❌ <b>Ошибка</b>"]
        if context:
            lines.append(f"📍 {context}")
        lines.append(f"⚠️ {error_msg[:500]}")
        return await self.send_message("\n".join(lines))

    async def send_startup(self, version: str = "0.1.0") -> bool:
        """Отправляет сообщение о запуске бота."""
        text = f"🤖 <b>Бот запущен</b>\n📌 Версия: {version}\n⏰ Ожидание расчёта в 06:30 МСК"
        return await self.send_message(text)