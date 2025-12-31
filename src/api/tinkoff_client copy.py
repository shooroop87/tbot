"""
Асинхронный клиент Tinkoff Invest API.

Документация:
- https://tinkoff.github.io/investAPI/
- https://developer.tbank.ru/docs/api
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import structlog
from t_tech.invest import (
    AsyncClient,
    CandleInterval,
    InstrumentStatus,
    SharesResponse,
    GetCandlesResponse,
)
from t_tech.invest.constants import INVEST_GRPC_API
from t_tech.invest.utils import quotation_to_decimal

from config import TinkoffConfig

logger = structlog.get_logger()


class TinkoffClient:
    """
    Обёртка над Tinkoff Invest API с поддержкой:
    - Async context manager
    - Rate limiting
    - Retry logic (TODO)
    """

    def __init__(self, config: TinkoffConfig):
        self.token = config.token
        self.account_id = config.account_id
        self._async_client: Optional[AsyncClient] = None
        self._services = None

    async def __aenter__(self) -> "TinkoffClient":
        """Вход в async context."""
        self._async_client = AsyncClient(
            token=self.token,
            target=INVEST_GRPC_API
        )
        self._services = await self._async_client.__aenter__()
        logger.info("tinkoff_client_connected", target="prod")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Выход из async context."""
        if self._async_client:
            await self._async_client.__aexit__(exc_type, exc_val, exc_tb)
        self._services = None
        self._async_client = None
        logger.info("tinkoff_client_disconnected")

    # ═══════════════════════════════════════════════════════════
    # Инструменты
    # ═══════════════════════════════════════════════════════════

    async def get_shares(self) -> List[Dict[str, Any]]:
        """Получает список всех акций на MOEX."""
        response: SharesResponse = await self._services.instruments.shares(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )

        shares = []
        for share in response.instruments:
            # RUB акции с любой MOEX-биржи
            if share.currency == "rub" and "moex" in share.exchange.lower():
                shares.append({
                    "figi": share.figi,
                    "ticker": share.ticker,
                    "name": share.name,
                    "lot": share.lot,
                    "min_price_increment": float(quotation_to_decimal(share.min_price_increment)),
                    "uid": share.uid,
                    "isin": share.isin,
                })

        logger.info("shares_loaded", count=len(shares))
        return shares

    async def get_futures_by_ticker(self, ticker_prefix: str = "Si") -> Optional[Dict[str, Any]]:
        """
        Получает ближайший фьючерс по префиксу тикера.
        
        Args:
            ticker_prefix: Префикс тикера (Si, Ri, BR и т.д.)
        
        Returns:
            Данные фьючерса или None
        """
        response = await self._services.instruments.futures(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )

        futures = []
        now = datetime.now()

        for fut in response.instruments:
            if not fut.ticker.startswith(ticker_prefix):
                continue
                
            expiration = fut.expiration_date.replace(tzinfo=None)
            
            if expiration <= now:
                continue

            futures.append({
                "figi": fut.figi,
                "ticker": fut.ticker,
                "name": fut.name,
                "lot": fut.lot,
                "min_price_increment": float(quotation_to_decimal(fut.min_price_increment)),
                "expiration": expiration,
                "basic_asset": fut.basic_asset,
                "uid": fut.uid,
            })

        futures.sort(key=lambda x: x["expiration"])
        
        if futures:
            fut = futures[0]
            logger.info("futures_by_ticker_found", ticker=fut["ticker"], expiration=fut["expiration"].strftime("%Y-%m-%d"))
            return fut
        return None

    # ═══════════════════════════════════════════════════════════
    # Свечи
    # ═══════════════════════════════════════════════════════════

    async def get_candles(
        self,
        figi: str,
        from_dt: datetime,
        to_dt: datetime,
        interval: CandleInterval = CandleInterval.CANDLE_INTERVAL_HOUR
    ) -> List[Dict[str, Any]]:
        """Получает исторические свечи."""
        candles = []
        current_from = from_dt

        max_days = 365 if interval == CandleInterval.CANDLE_INTERVAL_HOUR else 30

        while current_from < to_dt:
            current_to = min(current_from + timedelta(days=max_days), to_dt)

            try:
                response: GetCandlesResponse = await self._services.market_data.get_candles(
                    figi=figi,
                    from_=current_from,
                    to=current_to,
                    interval=interval,
                )

                for candle in response.candles:
                    if candle.is_complete:
                        candles.append({
                            "time": candle.time,
                            "open": float(quotation_to_decimal(candle.open)),
                            "high": float(quotation_to_decimal(candle.high)),
                            "low": float(quotation_to_decimal(candle.low)),
                            "close": float(quotation_to_decimal(candle.close)),
                            "volume": candle.volume,
                        })

                await asyncio.sleep(0.2)

            except Exception as e:
                logger.error("candles_error", figi=figi, error=str(e))
                await asyncio.sleep(1)

            current_from = current_to

        logger.debug("candles_loaded", figi=figi, count=len(candles))
        return candles

    # ═══════════════════════════════════════════════════════════
    # Цены и стакан
    # ═══════════════════════════════════════════════════════════

    async def get_last_price(self, figi: str) -> Optional[float]:
        """Получает последнюю цену."""
        try:
            response = await self._services.market_data.get_last_prices(figi=[figi])
            if response.last_prices:
                return float(quotation_to_decimal(response.last_prices[0].price))
        except Exception as e:
            logger.error("last_price_error", figi=figi, error=str(e))
        return None

    async def get_orderbook(self, figi: str, depth: int = 5) -> Optional[Dict[str, Any]]:
        """Получает стакан заявок."""
        try:
            response = await self._services.market_data.get_order_book(figi=figi, depth=depth)

            if response.bids and response.asks:
                best_bid = float(quotation_to_decimal(response.bids[0].price))
                best_ask = float(quotation_to_decimal(response.asks[0].price))
                spread_pct = (best_ask - best_bid) / best_bid * 100 if best_bid > 0 else 0

                return {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "spread_pct": round(spread_pct, 4),
                    "mid_price": (best_bid + best_ask) / 2,
                }
        except Exception as e:
            logger.error("orderbook_error", figi=figi, error=str(e))
        return None

    async def get_trading_status(self, figi: str) -> Optional[str]:
        """Проверяет торговый статус инструмента."""
        try:
            response = await self._services.market_data.get_trading_status(figi=figi)
            return response.trading_status.name
        except Exception as e:
            logger.error("trading_status_error", figi=figi, error=str(e))
        return None