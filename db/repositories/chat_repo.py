from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SeenMessage


class ChatRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def is_seen(self, funpay_msg_id: int) -> bool:
        result = await self._session.execute(
            select(SeenMessage).where(SeenMessage.funpay_msg_id == funpay_msg_id)
        )
        return result.scalar_one_or_none() is not None

    async def mark_seen(self, funpay_msg_id: int, chat_id: int) -> None:
        seen = SeenMessage(
            funpay_msg_id=funpay_msg_id,
            chat_id=chat_id,
            processed_at=datetime.utcnow(),
        )
        self._session.add(seen)
        await self._session.commit()
