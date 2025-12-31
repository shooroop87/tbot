#!/usr/bin/env python3
"""Получение списка аккаунтов Tinkoff."""
import asyncio
import os

from t_tech.invest import AsyncClient
from t_tech.invest.constants import INVEST_GRPC_API

TOKEN = os.getenv("TINKOFF_TOKEN")

async def main():
    if not TOKEN:
        print("❌ TINKOFF_TOKEN не задан!")
        return
    
    async with AsyncClient(token=TOKEN, target=INVEST_GRPC_API) as client:
        accounts = await client.users.get_accounts()
        
        print("=" * 60)
        print("ВАШИ АККАУНТЫ TINKOFF:")
        print("=" * 60)
        
        for acc in accounts.accounts:
            print(f"\n📌 Account ID: {acc.id}")
            print(f"   Название: {acc.name}")
            print(f"   Тип: {acc.type.name}")
            print(f"   Статус: {acc.status.name}")
            print(f"   Доступ: {acc.access_level.name}")
        
        print("\n" + "=" * 60)
        print("Скопируй нужный Account ID и добавь в .env:")
        print("TINKOFF_ACCOUNT_ID=xxxxxxxxxx")
        print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())