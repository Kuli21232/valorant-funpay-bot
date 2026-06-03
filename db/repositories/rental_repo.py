from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import RentalEndReason, RentalSession


class RentalRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(
        self,
        account_id: int,
        buyer_funpay_id: int,
        buyer_username: str,
        order_funpay_id: str,
        chat_id: int,
        rental_minutes: int,
        price: Optional[float] = None,
    ) -> RentalSession:
        now = datetime.utcnow()
        rental = RentalSession(
            account_id=account_id,
            buyer_funpay_id=buyer_funpay_id,
            buyer_username=buyer_username,
            order_funpay_id=order_funpay_id,
            chat_id=chat_id,
            started_at=now,
            ends_at=now + timedelta(minutes=rental_minutes),
            price=price,
        )
        self._session.add(rental)
        await self._session.commit()
        await self._session.refresh(rental)
        return rental

    async def get_by_id(self, rental_id: int) -> Optional[RentalSession]:
        return await self._session.get(RentalSession, rental_id)

    async def get_active(self) -> list[RentalSession]:
        result = await self._session.execute(
            select(RentalSession).where(RentalSession.ended_at.is_(None))
        )
        return list(result.scalars().all())

    async def get_active_by_account(self, account_id: int) -> Optional[RentalSession]:
        result = await self._session.execute(
            select(RentalSession).where(
                RentalSession.account_id == account_id,
                RentalSession.ended_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_expired(self) -> list[RentalSession]:
        """Active rentals whose time is up."""
        now = datetime.utcnow()
        result = await self._session.execute(
            select(RentalSession).where(
                RentalSession.ended_at.is_(None),
                RentalSession.ends_at <= now,
            )
        )
        return list(result.scalars().all())

    async def close(
        self, rental_id: int, reason: RentalEndReason
    ) -> Optional[RentalSession]:
        rental = await self.get_by_id(rental_id)
        if rental and rental.ended_at is None:
            rental.ended_at = datetime.utcnow()
            rental.ended_reason = reason
            await self._session.commit()
            await self._session.refresh(rental)
        return rental
