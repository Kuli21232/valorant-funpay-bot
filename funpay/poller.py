import asyncio
import logging
from typing import Callable, Optional

import httpx

from config import settings
from db.database import AsyncSessionLocal
from db.models import OrderStatus
from db.repositories.chat_repo import ChatRepository
from db.repositories.order_repo import OrderRepository
from funpay.client import FunPayAuthError, FunPayClient
from funpay.parser import parse_chat_bookmarks, parse_messages_from_html, parse_orders_from_html
from funpay.types import FPEvent

logger = logging.getLogger(__name__)


class FunPayPoller:
    def __init__(self, client: FunPayClient, event_queue: asyncio.Queue):
        self._client = client
        self._queue = event_queue
        self._chat_tags: dict[int, str] = {}
        self._order_tags: str = "0"
        self._known_orders: dict[str, str] = {}
        self._first_poll: bool = True

    async def run(self) -> None:
        logger.info("FunPay poller started")
        while True:
            try:
                await self._poll()
            except FunPayAuthError as e:
                logger.error("FunPay auth error: %s", e)
                await self._emit("auth_error", {"message": str(e)})
                await asyncio.sleep(60)
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                logger.warning(
                    "FunPay network error: %s — will retry in %ds",
                    type(e).__name__, settings.FUNPAY_POLL_INTERVAL,
                )
            except Exception as e:
                logger.exception("Poller error: %s", e)
            await asyncio.sleep(settings.FUNPAY_POLL_INTERVAL)

    async def _poll(self) -> None:
        objects = [
            {
                "type": "orders_counters",
                "id": str(settings.FUNPAY_USER_ID),
                "tag": self._order_tags,
                "data": False,
            },
            {
                "type": "chat_bookmarks",
                "id": str(settings.FUNPAY_USER_ID),
                "tag": "0",
                "data": False,
            },
        ]
        data = await self._client.runner_request(objects)
        updates = data.get("objects", [])

        for update in updates:
            obj_type = update.get("type")
            if obj_type == "orders_counters":
                await self._handle_orders_update(update)
            elif obj_type == "chat_bookmarks":
                await self._handle_chats_update(update)

    async def _handle_orders_update(self, update: dict) -> None:
        new_tag = update.get("tag", self._order_tags)
        if new_tag == self._order_tags and self._order_tags != "0":
            return
        self._order_tags = new_tag

        html = await self._client.fetch_orders_page()
        orders = parse_orders_from_html(html)

        async with AsyncSessionLocal() as session:
            order_repo = OrderRepository(session)

            # On first poll, also seed _known_orders from DB so previously seen
            # orders don't generate "new_order" events.
            if self._first_poll and not self._known_orders:
                from sqlalchemy import select
                from db.models import Order as OrderModel
                result = await session.execute(select(OrderModel))
                for o in result.scalars().all():
                    self._known_orders[o.funpay_id] = o.status

            for fp_order in orders:
                old_status = self._known_orders.get(fp_order.id)
                existing = await order_repo.get_by_funpay_id(fp_order.id)

                if old_status is None and existing is None:
                    # Brand new order — save to DB
                    await order_repo.create_or_update(
                        funpay_id=fp_order.id,
                        buyer_id=fp_order.buyer_id,
                        buyer_username=fp_order.buyer_username,
                        chat_id=fp_order.chat_id,
                        status=fp_order.status,
                        price=fp_order.price,
                        description=fp_order.description,
                    )
                    # Emit event only if not the first poll (avoid flooding on startup)
                    if not self._first_poll:
                        await self._emit("new_order", {"order": fp_order})
                        logger.info("New order detected: %s", fp_order.id)
                    else:
                        logger.debug("Seeding existing order: %s", fp_order.id)
                elif old_status is not None and old_status != fp_order.status:
                    await order_repo.update_status(fp_order.id, fp_order.status)
                    if not self._first_poll:
                        await self._emit(
                            "order_status_changed",
                            {
                                "order_id": fp_order.id,
                                "old_status": old_status,
                                "new_status": fp_order.status,
                            },
                        )
                self._known_orders[fp_order.id] = fp_order.status

        if self._first_poll:
            logger.info(
                "Initial poll complete — %d orders seeded, future events will be emitted normally",
                len(self._known_orders),
            )
            self._first_poll = False

    async def _handle_chats_update(self, update: dict) -> None:
        chats_data = update.get("data", {})
        if not chats_data:
            return

        items = chats_data if isinstance(chats_data, list) else []
        chats = parse_chat_bookmarks(items)

        for chat in chats:
            if not chat.unread:
                continue
            await self._fetch_and_process_chat(chat.id, chat.name)

    async def _fetch_and_process_chat(self, chat_id: int, chat_name: str) -> None:
        try:
            html = await self._client.fetch_chat_history(chat_id)
            messages = parse_messages_from_html(html, chat_id, settings.FUNPAY_USER_ID)

            async with AsyncSessionLocal() as session:
                chat_repo = ChatRepository(session)
                for msg in messages:
                    if not msg.is_incoming:
                        continue
                    if await chat_repo.is_seen(msg.id):
                        continue
                    await chat_repo.mark_seen(msg.id, chat_id)
                    await self._emit("new_message", {"message": msg})
                    logger.info(
                        "New message from %s (chat %d): %s",
                        chat_name,
                        chat_id,
                        msg.text[:60],
                    )
        except Exception as e:
            logger.exception("Error fetching chat %d: %s", chat_id, e)

    async def _emit(self, event_type: str, payload: dict) -> None:
        await self._queue.put(FPEvent(type=event_type, payload=payload))
