"""
Менеджер заявок Tinkoff API.

Поддерживает:
- Отложенные заявки (тейк-профит на покупку) - невидимы в стакане
- Отмена заявок
- Получение списка активных заявок
"""
from typing import Optional, Dict, Any, List
from decimal import Decimal

import structlog
from t_tech.invest import (
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
)
from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

logger = structlog.get_logger()


class OrderManager:
    """
    Менеджер заявок.
    
    Пример использования:
        >>> async with TinkoffClient(config.tinkoff) as client:
        ...     manager = OrderManager(client, config)
        ...     result = await manager.place_take_profit_buy(
        ...         figi="BBG004730N88",
        ...         quantity=100,
        ...         price=275.50
        ...     )
    """

    def __init__(self, tinkoff_client, config):
        self.client = tinkoff_client
        self.config = config
        self.account_id = config.tinkoff.account_id
        self.logger = logger.bind(component="order_manager")

    async def place_take_profit_buy(
        self,
        figi: str,
        quantity: int,
        price: float,
    ) -> Dict[str, Any]:
        """
        Выставляет отложенную заявку Тейк-профит на ПОКУПКУ.
        
        Заявка НЕВИДИМА в стакане!
        Сработает когда цена ОПУСТИТСЯ до указанной цены.
        
        Args:
            figi: FIGI инструмента
            quantity: Количество лотов
            price: Цена активации
        
        Returns:
            Dict с результатом
        """
        self.logger.info("place_take_profit_buy_called",
                        figi=figi,
                        quantity=quantity,
                        price=price,
                        account_id=self.account_id,
                        dry_run=self.config.dry_run)
        
        # Проверка account_id
        if not self.account_id:
            self.logger.error("no_account_id", message="TINKOFF_ACCOUNT_ID не указан")
            return {
                "success": False,
                "error": "TINKOFF_ACCOUNT_ID не указан в .env"
            }
        
        # Проверка quantity
        if quantity <= 0:
            self.logger.error("invalid_quantity", quantity=quantity)
            return {
                "success": False,
                "error": f"Некорректное количество лотов: {quantity}"
            }
        
        # Dry run режим
        if self.config.dry_run:
            self.logger.warning("dry_run_mode", 
                              action="place_take_profit_buy",
                              figi=figi,
                              quantity=quantity,
                              price=price)
            return {
                "success": True,
                "dry_run": True,
                "order_id": "DRY_RUN_ORDER_ID",
                "message": f"Заявка НЕ выставлена (dry_run=True). Было бы: TAKE_PROFIT BUY {quantity} лот(ов) по {price}"
            }
        
        try:
            self.logger.info("calling_tinkoff_api", action="post_stop_order")
            services = self.client._services
            
            # Выставляем отложенную заявку тейк-профит на покупку
            response = await services.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=decimal_to_quotation(Decimal(str(price))),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_BUY,
                account_id=self.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            
            self.logger.info("take_profit_buy_placed",
                           order_id=response.stop_order_id,
                           figi=figi,
                           quantity=quantity,
                           price=price)
            
            return {
                "success": True,
                "order_id": response.stop_order_id,
            }
            
        except Exception as e:
            self.logger.exception("order_error", figi=figi, error=str(e))
            return {
                "success": False,
                "error": str(e)
            }

    async def cancel_stop_order(self, order_id: str) -> Dict[str, Any]:
        """Отменяет стоп-заявку."""
        self.logger.info("cancel_stop_order_called", order_id=order_id)
        
        if self.config.dry_run:
            return {"success": True, "dry_run": True}
            
        try:
            services = self.client._services
            await services.stop_orders.cancel_stop_order(
                account_id=self.account_id,
                stop_order_id=order_id
            )
            self.logger.info("stop_order_cancelled", order_id=order_id)
            return {"success": True}
        except Exception as e:
            self.logger.error("cancel_stop_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def get_stop_orders(self) -> List[Dict[str, Any]]:
        """Получает список активных стоп-заявок."""
        try:
            services = self.client._services
            response = await services.stop_orders.get_stop_orders(
                account_id=self.account_id
            )
            
            orders = []
            for order in response.stop_orders:
                orders.append({
                    "order_id": order.stop_order_id,
                    "figi": order.figi,
                    "direction": order.direction.name,
                    "price": float(quotation_to_decimal(order.stop_price)),
                    "quantity": order.lots_requested,
                    "status": order.status.name,
                    "order_type": order.stop_order_type.name,
                })
            
            self.logger.debug("stop_orders_fetched", count=len(orders))
            return orders
            
        except Exception as e:
            self.logger.error("get_stop_orders_error", error=str(e))
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Получает текущие позиции в портфеле."""
        try:
            services = self.client._services
            response = await services.operations.get_portfolio(
                account_id=self.account_id
            )
            
            positions = []
            for pos in response.positions:
                qty = float(quotation_to_decimal(pos.quantity))
                if qty > 0:
                    positions.append({
                        "figi": pos.figi,
                        "quantity": qty,
                        "average_price": float(quotation_to_decimal(pos.average_position_price)),
                        "current_price": float(quotation_to_decimal(pos.current_price)),
                        "expected_yield": float(quotation_to_decimal(pos.expected_yield)),
                    })
            
            self.logger.debug("positions_fetched", count=len(positions))
            return positions
            
        except Exception as e:
            self.logger.error("get_positions_error", error=str(e))
            return []