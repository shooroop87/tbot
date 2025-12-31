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
        ...         activation_price=29.61
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
        activation_price: float,
    ) -> Dict[str, Any]:
        """
        Выставляет отложенную заявку Тейк-профит на ПОКУПКУ.
        
        Заявка невидима в стакане!
        Сработает когда цена коснётся activation_price.
        
        Args:
            figi: FIGI инструмента
            quantity: Количество лотов
            activation_price: Цена активации (BB Lower)
        
        Returns:
            Dict с результатом:
            - success: True/False
            - stop_order_id: ID заявки (если успешно)
            - error: текст ошибки (если неуспешно)
        """
        # Проверка account_id
        if not self.account_id:
            self.logger.error("no_account_id")
            return {
                "success": False,
                "error": "TINKOFF_ACCOUNT_ID не указан в .env"
            }
        
        # Dry run режим
        if self.config.dry_run:
            self.logger.warning("dry_run_mode", 
                              action="place_take_profit_buy",
                              figi=figi,
                              quantity=quantity,
                              price=activation_price)
            return {
                "success": True,
                "dry_run": True,
                "stop_order_id": "DRY_RUN_ORDER_ID",
                "message": f"Заявка НЕ выставлена (dry_run=True). Было бы: BUY {quantity} шт по {activation_price}"
            }
        
        try:
            services = self.client._services
            
            # Выставляем отложенную заявку тейк-профит на покупку
            response = await services.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=decimal_to_quotation(Decimal(str(activation_price))),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_BUY,
                account_id=self.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            
            self.logger.info("take_profit_buy_placed",
                           stop_order_id=response.stop_order_id,
                           figi=figi,
                           quantity=quantity,
                           activation_price=activation_price)
            
            return {
                "success": True,
                "stop_order_id": response.stop_order_id,
            }
            
        except Exception as e:
            self.logger.exception("order_error", figi=figi)
            return {
                "success": False,
                "error": str(e)
            }

    async def cancel_stop_order(self, stop_order_id: str) -> Dict[str, Any]:
        """
        Отменяет стоп-заявку.
        
        Args:
            stop_order_id: ID стоп-заявки
        
        Returns:
            Dict с результатом
        """
        if self.config.dry_run:
            self.logger.warning("dry_run_mode", action="cancel_stop_order")
            return {"success": True, "dry_run": True}
            
        try:
            services = self.client._services
            await services.stop_orders.cancel_stop_order(
                account_id=self.account_id,
                stop_order_id=stop_order_id
            )
            self.logger.info("stop_order_cancelled", stop_order_id=stop_order_id)
            return {"success": True}
        except Exception as e:
            self.logger.error("cancel_stop_error", error=str(e))
            return {"success": False, "error": str(e)}

    async def get_stop_orders(self) -> List[Dict[str, Any]]:
        """
        Получает список активных стоп-заявок.
        
        Returns:
            Список заявок с полями: stop_order_id, figi, direction, 
            stop_price, quantity, status
        """
        try:
            services = self.client._services
            response = await services.stop_orders.get_stop_orders(
                account_id=self.account_id
            )
            
            orders = []
            for order in response.stop_orders:
                orders.append({
                    "stop_order_id": order.stop_order_id,
                    "figi": order.figi,
                    "direction": order.direction.name,
                    "stop_price": float(quotation_to_decimal(order.stop_price)),
                    "quantity": order.lots_requested,
                    "status": order.status.name,
                    "order_type": order.stop_order_type.name,
                    "create_date": order.create_date,
                })
            
            self.logger.debug("stop_orders_fetched", count=len(orders))
            return orders
            
        except Exception as e:
            self.logger.error("get_stop_orders_error", error=str(e))
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Получает текущие позиции в портфеле.
        
        Returns:
            Список позиций
        """
        try:
            services = self.client._services
            response = await services.operations.get_portfolio(
                account_id=self.account_id
            )
            
            positions = []
            for pos in response.positions:
                if float(quotation_to_decimal(pos.quantity)) > 0:
                    positions.append({
                        "figi": pos.figi,
                        "quantity": float(quotation_to_decimal(pos.quantity)),
                        "average_price": float(quotation_to_decimal(pos.average_position_price)),
                        "current_price": float(quotation_to_decimal(pos.current_price)),
                        "expected_yield": float(quotation_to_decimal(pos.expected_yield)),
                    })
            
            self.logger.debug("positions_fetched", count=len(positions))
            return positions
            
        except Exception as e:
            self.logger.error("get_positions_error", error=str(e))
            return []