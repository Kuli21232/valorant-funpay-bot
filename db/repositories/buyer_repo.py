from datetime import datetime
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Buyer


class BuyerRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_funpay_id(self, funpay_id: int) -> Optional[Buyer]:
        result = await self._session.execute(
            select(Buyer).where(Buyer.funpay_id == funpay_id)
        )
        return result.scalar_one_or_none()

    async def upsert(self, funpay_id: int, username: str) -> Buyer:
        buyer = await self.get_by_funpay_id(funpay_id)
        if buyer:
            buyer.username = username  # update in case it changed
            await self._session.commit()
            await self._session.refresh(buyer)
            return buyer
        buyer = Buyer(funpay_id=funpay_id, username=username)
        self._session.add(buyer)
        await self._session.commit()
        await self._session.refresh(buyer)
        return buyer

    async def record_rental(
        self,
        funpay_id: int,
        username: str,
        minutes: int,
        price: float = 0.0,
    ) -> Buyer:
        buyer = await self.upsert(funpay_id, username)
        buyer.total_orders += 1
        buyer.total_minutes_rented += minutes
        buyer.total_spent += price
        buyer.last_order_at = datetime.utcnow()
        await self._session.commit()
        await self._session.refresh(buyer)
        return buyer

    async def top(self, limit: int = 10) -> list[Buyer]:
        result = await self._session.execute(
            select(Buyer).order_by(desc(Buyer.total_spent)).limit(limit)
        )
        return list(result.scalars().all())

    async def get_all(self) -> list[Buyer]:
        result = await self._session.execute(
            select(Buyer).order_by(desc(Buyer.last_order_at))
        )
        return list(result.scalars().all())
