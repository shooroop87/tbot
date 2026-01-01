"""
Отправка уведомлений в Telegram.

Поддерживает:
- Ежедневные отчёты
- Сигналы на вход/выход
- Результаты сделок
- Алерты ошибок
"""
import asyncio
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
        disable_notification: bool = False,
    ) -> bool:
        """
        Отправляет сообщение в Telegram.
        
        Args:
            text: Текст сообщения (поддерживает HTML)
            parse_mode: Формат (HTML/Markdown)
            disable_notification: Без звука
        
        Returns:
            True если успешно
        """
        # Telegram лимит: 4096 символов
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (сообщение обрезано)"

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
            return f"{price:.2f}"
        else:
            return f"{price:.3f}"

    async def send_daily_report(self, report: Dict[str, Any]) -> bool:
        """Отправляет ежедневный отчёт."""
        # Заголовок
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
            lines.append(f"📋 Ликвидные: {', '.join(liquid_shares[:15])}...")
        
        lines.append("")
        lines.append("💡 Для заявки: <code>/buy TICKER</code>")
        lines.append("")
        
        await self.send_message("\n".join(lines))
        
        # Отправляем каждую акцию отдельным сообщением
        top_shares = report.get("top_shares", [])
        if top_shares:
            for share in top_shares:
                await self._send_share_card(share)
                await asyncio.sleep(0.3)
        else:
            await self.send_message("<i>Нет акций с достаточными данными</i>")
        
        # Фьючерс Si
        futures_si = report.get("futures_si")
        if futures_si:
            await self._send_futures_card(futures_si)
        
        # Подвал
        footer = [
            "━━━━━━━━━━━━━━━━━━━━",
            "⚠️ <i>Торговля несёт риск потери капитала</i>",
            f"🤖 dry_run: {report.get('dry_run', True)}",
        ]
        await self.send_message("\n".join(footer))
        
        return True

    async def _send_share_card(self, share: Dict[str, Any]) -> bool:
        """Отправляет карточку акции."""
        emoji = "🟢" if share.get("signal") == "BUY" else "⚪"
        
        lines = [
            f"{emoji} <b>{share['ticker']}</b> — <code>/buy {share['ticker']}</code>",
            f"   💵 Цена: {self._format_price(share['price'])} ₽",
            f"   📊 ATR: {self._format_price(share['atr'])} ({share['atr_pct']:.1f}%)",
            f"   📉 BB нижняя: {self._format_price(share['bb_lower'])} ₽",
            "",
            f"   <b>R:R 1:3 параметры:</b>",
            f"   📥 Вход: {self._format_price(share['entry_price'])} ₽",
            f"   🎯 Тейк (+{self._format_price(share['take_offset'])}): {self._format_price(share['take_price'])} ₽",
            f"   🛑 Стоп (-{self._format_price(share['stop_offset'])}): {self._format_price(share['stop_price'])} ₽",
            "",
            f"   📦 Позиция: {share['position_size']:,} шт ({share['position_value']:,.0f} ₽)",
            f"   💸 Потенц. убыток: {share['potential_loss']:,.0f} ₽",
            f"   💰 Потенц. прибыль: {share['potential_profit']:,.0f} ₽",
            f"   📏 До BB: {share.get('distance_to_bb_pct', 0):.1f}%",
        ]
        
        return await self.send_message("\n".join(lines))

    async def _send_futures_card(self, futures: Dict[str, Any]) -> bool:
        """Отправляет карточку фьючерса."""
        lines = [
            f"<b>💵 Фьючерс {futures['ticker']}</b> — <code>/buy {futures['ticker']}</code>",
            f"   Цена: {futures['price']:,.0f}",
            f"   📊 ATR: {futures.get('atr', 0):,.0f} ({futures.get('atr_pct', 0):.1f}%)",
            f"   📉 BB нижняя: {futures['bb_lower']:,.0f}",
            "",
        ]
        
        # R:R параметры если есть
        if futures.get('entry_price'):
            lines.extend([
                f"   <b>R:R 1:3 параметры:</b>",
                f"   📥 Вход: {futures['entry_price']:,.0f}",
                f"   🎯 Тейк (+{futures.get('take_offset', 0):,.0f}): {futures.get('take_price', 0):,.0f}",
                f"   🛑 Стоп (-{futures.get('stop_offset', 0):,.0f}): {futures.get('stop_price', 0):,.0f}",
                "",
                f"   📦 Позиция: {futures.get('position_size', 0):,} шт ({futures.get('position_value', 0):,.0f} ₽)",
                f"   💸 Потенц. убыток: {futures.get('potential_loss', 0):,.0f} ₽",
                f"   💰 Потенц. прибыль: {futures.get('potential_profit', 0):,.0f} ₽",
            ])
        
        lines.append(f"   📅 Экспирация: {futures['expiration']}")
        
        return await self.send_message("\n".join(lines))

    async def send_signal(
        self,
        ticker: str,
        signal_type: str,
        price: float,
        target: Optional[float] = None,
        stop: Optional[float] = None,
        size: Optional[int] = None,
        reason: str = ""
    ) -> bool:
        """Отправляет торговый сигнал."""
        emoji_map = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "⚪"}
        emoji = emoji_map.get(signal_type, "⚪")

        lines = [
            f"{emoji} <b>{signal_type}</b> {ticker}",
            f"💰 Цена: {price:,.2f} ₽",
        ]

        if size:
            lines.append(f"📦 Объём: {size} шт")
        if target:
            lines.append(f"🎯 Цель: {target:,.2f} ₽")
        if stop:
            lines.append(f"🛑 Стоп: {stop:,.2f} ₽")
        if reason:
            lines.append(f"📝 {reason}")

        return await self.send_message("\n".join(lines))

    async def send_trade_result(
        self,
        ticker: str,
        entry_price: float,
        exit_price: float,
        size: int,
        pnl_rub: float,
        pnl_pct: float,
        reason: str = ""
    ) -> bool:
        """Отправляет результат закрытой сделки."""
        emoji = "✅" if pnl_rub >= 0 else "❌"

        lines = [
            f"{emoji} <b>Сделка закрыта:</b> {ticker}",
            f"📥 Вход: {entry_price:,.2f} ₽",
            f"📤 Выход: {exit_price:,.2f} ₽",
            f"📦 Объём: {size} шт",
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
        text = f"🤖 <b>Бот запущен</b>\n📌 Версия: {version}\n⏰ Ожидание расчёта в 06:30 МСК\n\n💡 Команды: /help"
        return await self.send_message(text)

    async def send_order_confirmation(
        self,
        ticker: str,
        order_type: str,
        price: float,
        quantity: int,
        order_id: str
    ) -> bool:
        """Отправляет подтверждение выставленной заявки."""
        lines = [
            f"✅ <b>Заявка выставлена</b>",
            f"📌 {ticker}",
            f"📋 Тип: {order_type}",
            f"💰 Цена активации: {self._format_price(price)} ₽",
            f"📦 Количество: {quantity:,} шт",
            f"🆔 ID: <code>{order_id}</code>",
        ]
        return await self.send_message("\n".join(lines))

    async def send_order_error(self, ticker: str, error: str) -> bool:
        """Отправляет сообщение об ошибке заявки."""
        # Экранируем HTML-опасные символы
        safe_error = error.replace("<", "&lt;").replace(">", "&gt;")[:500]
        lines = [
            f"❌ <b>Ошибка заявки</b>",
            f"📌 {ticker}",
            f"⚠️ {safe_error}",
        ]
        return await self.send_message("\n".join(lines))