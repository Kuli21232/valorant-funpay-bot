from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Order, OrderStatus


class OrderRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_funpay_id(self, funpay_id: str) -> Optional[Order]:
        result = await self._session.execute(
            select(Order).where(Order.funpay_id == funpay_id)
        )
        return result.scalar_one_or_none()

    async def get_active_by_account(self, account_id: int) -> Optional[Order]:
        result = await self._session.execute(
            select(Order).where(
                Order.account_id == account_id,
                Order.status == OrderStatus.ACTIVE,
            )
        )
        return result.scalar_one_or_none()

    async def create_or_update(
        self,
        funpay_id: str,
        buyer_id: int,
        buyer_username: str,
        chat_id: int,
        status: str,
        price: Optional[float] = None,
        currency: str = "RUB",
        description: Optional[str] = None,
        rental_minutes: Optional[int] = None,
    ) -> Order:
        order = await self.get_by_funpay_id(funpay_id)
        if order:
            order.status = status
            await self._session.commit()
            await self._session.refresh(order)
            return order

        order = Order(
            funpay_id=funpay_id,
            buyer_id=buyer_id,
            buyer_username=buyer_username,
            chat_id=chat_id,
            status=status,
            price=price,
            currency=currency,
            description=description,
            rental_minutes=rental_minutes,
        )
        self._session.add(order)
        await self._session.commit()
        await self._session.refresh(order)
        return order

    async def mark_delivered(self, funpay_id: str, account_id: int) -> None:
        order = await self.get_by_funpay_id(funpay_id)
        if order:
            order.delivered = True
            order.account_id = account_id
            order.status = OrderStatus.ACTIVE
            await self._session.commit()

    async def update_status(self, funpay_id: str, status: str) -> Optional[Order]:
        order = await self.get_by_funpay_id(funpay_id)
        if order:
            order.status = status
            await self._session.commit()
            await self._session.refresh(order)
        return order

    async def mark_auto_replied(self, funpay_id: str) -> None:
        order = await self.get_by_funpay_id(funpay_id)
        if order:
            order.auto_replied = True
            await self._session.commit()

    async def assign_account(self, funpay_id: str, account_id: int) -> Optional[Order]:
        order = await self.get_by_funpay_id(funpay_id)
        if order:
            order.account_id = account_id
            order.status = OrderStatus.ACTIVE
            await self._session.commit()
            await self._session.refresh(order)
        return order
