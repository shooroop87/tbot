#!/usr/bin/env python3
"""
Тест: выставление заявки TAKE_PROFIT на покупку.

Запуск:
    python tests/test_order.py

⚠️ ВНИМАНИЕ: Выставит РЕАЛЬНУЮ заявку если dry_run=False!
"""
import asyncio
import os
from decimal import Decimal

from dotenv import load_dotenv
load_dotenv()

from t_tech.invest import AsyncClient
from t_tech.invest.constants import INVEST_GRPC_API
from t_tech.invest import (
    StopOrderDirection,
    StopOrderType,
    StopOrderExpirationType,
    InstrumentStatus,
)
from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

TOKEN = os.getenv("TINKOFF_TOKEN")
ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID")

# Параметры тестовой заявки
TEST_TICKER = "CNRU"
TEST_PRICE = 500.0  # Цена активации (ниже текущей ~600)
TEST_LOTS = 1  # 1 лот


async def main():
    print("=" * 60)
    print("ТЕСТ ВЫСТАВЛЕНИЯ ЗАЯВКИ TAKE_PROFIT")
    print("=" * 60)
    
    if not TOKEN:
        print("❌ TINKOFF_TOKEN не задан!")
        return
    
    if not ACCOUNT_ID:
        print("❌ TINKOFF_ACCOUNT_ID не задан!")
        return
    
    print(f"📌 Account ID: {ACCOUNT_ID}")
    print(f"📌 Тикер: {TEST_TICKER}")
    print(f"📌 Цена активации: {TEST_PRICE}")
    print(f"📌 Лотов: {TEST_LOTS}")
    print()
    
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as client:
        
        # 1. Находим FIGI по тикеру
        print("🔍 Ищем инструмент...")
        shares = await client.instruments.shares(
            instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE
        )
        
        share = None
        for s in shares.instruments:
            if s.ticker == TEST_TICKER:
                share = s
                break
        
        if not share:
            print(f"❌ Тикер {TEST_TICKER} не найден!")
            return
        
        print(f"✅ Найден: {share.name}")
        print(f"   FIGI: {share.figi}")
        print(f"   Лот: {share.lot} акций")
        print()
        
        # 2. Получаем текущую цену
        print("💰 Получаем текущую цену...")
        prices = await client.market_data.get_last_prices(figi=[share.figi])
        current_price = float(quotation_to_decimal(prices.last_prices[0].price))
        print(f"   Текущая цена: {current_price}")
        print()
        
        # 3. Проверяем что цена активации ниже текущей (для TAKE_PROFIT BUY)
        if TEST_PRICE >= current_price:
            print(f"⚠️  Цена активации ({TEST_PRICE}) должна быть НИЖЕ текущей ({current_price})")
            print("   Для TAKE_PROFIT BUY заявка сработает когда цена ОПУСТИТСЯ до указанной")
            return
        
        # 4. Смотрим текущие стоп-заявки
        print("📋 Текущие стоп-заявки:")
        stop_orders = await client.stop_orders.get_stop_orders(account_id=ACCOUNT_ID)
        if stop_orders.stop_orders:
            for order in stop_orders.stop_orders:
                print(f"   - {order.figi}: {order.stop_order_type.name} @ {float(quotation_to_decimal(order.stop_price))}")
        else:
            print("   (нет активных)")
        print()
        
        # 5. Спрашиваем подтверждение
        print("=" * 60)
        print("⚠️  ВНИМАНИЕ: Сейчас будет выставлена РЕАЛЬНАЯ заявка!")
        print(f"   TAKE_PROFIT BUY {TEST_LOTS} лот(ов) {TEST_TICKER} по {TEST_PRICE}")
        print("=" * 60)
        
        confirm = input("\nВведите 'YES' для подтверждения: ")
        if confirm != "YES":
            print("❌ Отменено")
            return
        
        # 6. Выставляем заявку
        print()
        print("📤 Выставляем заявку...")
        
        try:
            response = await client.stop_orders.post_stop_order(
                figi=share.figi,
                quantity=TEST_LOTS,
                stop_price=decimal_to_quotation(Decimal(str(TEST_PRICE))),
                direction=StopOrderDirection.STOP_ORDER_DIRECTION_BUY,
                account_id=ACCOUNT_ID,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
            )
            
            print()
            print("✅ ЗАЯВКА ВЫСТАВЛЕНА!")
            print(f"   Order ID: {response.stop_order_id}")
            print()
            
            # 7. Проверяем что заявка появилась
            print("📋 Проверяем заявки после выставления...")
            stop_orders = await client.stop_orders.get_stop_orders(account_id=ACCOUNT_ID)
            for order in stop_orders.stop_orders:
                if order.stop_order_id == response.stop_order_id:
                    print(f"   ✅ Заявка найдена: {order.stop_order_type.name}")
                    print(f"      Статус: {order.status.name}")
                    print(f"      Цена: {float(quotation_to_decimal(order.stop_price))}")
            
            # 8. Предлагаем отменить
            print()
            cancel = input("Отменить заявку? (YES/no): ")
            if cancel == "YES":
                await client.stop_orders.cancel_stop_order(
                    account_id=ACCOUNT_ID,
                    stop_order_id=response.stop_order_id
                )
                print("✅ Заявка отменена")
            
        except Exception as e:
            print(f"❌ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())