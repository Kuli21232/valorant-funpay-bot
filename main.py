import asyncio
import atexit
import logging
import os
import sys
from pathlib import Path

from bot.main import create_bot
from bot.services.notification_service import NotificationService
from db.database import init_db
from funpay.client import FunPayAuthError, FunPayClient
from funpay.poller import FunPayPoller
from funpay.sender import FunPaySender
from funpay.types import FPEvent, FPMessage, FPOrder
from rental.delivery import RentalDeliveryService
from rental.qr_pipeline import QrPipeline
from rental.scheduler import RentalScheduler
from utils.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(
        self,
        queue: asyncio.Queue,
        client: FunPayClient,
        notification_service: NotificationService,
        delivery_service: RentalDeliveryService,
        qr_pipeline: QrPipeline,
    ):
        self._queue = queue
        self._client = client
        self._notifier = notification_service
        self._sender = FunPaySender()
        self._delivery = delivery_service
        self._qr = qr_pipeline

    async def run(self) -> None:
        logger.info("Event dispatcher started")
        while True:
            try:
                event: FPEvent = await self._queue.get()
                await self._dispatch(event)
                self._queue.task_done()
            except Exception as e:
                logger.exception("Dispatcher error: %s", e)

    async def _dispatch(self, event: FPEvent) -> None:
        if event.type == "new_message":
            await self._handle_new_message(event.payload.get("message"))
        elif event.type == "new_order":
            await self._handle_new_order(event.payload.get("order"))
        elif event.type == "order_status_changed":
            await self._handle_status_changed(event.payload)
        elif event.type == "auth_error":
            await self._notifier.notify_auth_error(event.payload.get("message", ""))

    async def _handle_new_message(self, msg: FPMessage | None) -> None:
        if not msg:
            return

        # !помощь — call the seller via Telegram
        if msg.text.strip() == "!помощь":
            await self._sender.send_help_acknowledgement(self._client, msg.chat_id)
            await self._notifier.notify_help_command(
                chat_id=msg.chat_id,
                buyer_name=msg.chat_name,
                message_text=msg.text,
            )
            return

        # Buyer sent an image — could be the Valorant QR for sign-in
        if msg.image_urls and msg.is_incoming:
            try:
                handled = await self._qr.handle_message(msg)
                logger.info("QR pipeline handled=%s for msg %d (chat %d)",
                            handled, msg.id, msg.chat_id)
            except Exception as e:
                logger.exception("QR pipeline error on msg %d: %s", msg.id, e)

    async def _handle_new_order(self, order: FPOrder | None) -> None:
        if not order:
            return
        await self._notifier.notify_new_order(order)
        # Auto-deliver: assign a READY account and send credentials
        try:
            await self._delivery.deliver(order)
        except Exception as e:
            logger.exception("Auto-delivery failed for order %s: %s", order.id, e)

    async def _handle_status_changed(self, payload: dict) -> None:
        order_id = payload.get("order_id", "")
        new_status = payload.get("new_status", "")
        old_status = payload.get("old_status", "")

        # Always notify
        await self._notifier.notify_order_status_changed(
            order_id=order_id, old_status=old_status, new_status=new_status,
        )

        # Special handling: dispute opened → alert admin with action buttons
        if new_status == "dispute":
            from db.database import AsyncSessionLocal
            from db.repositories.order_repo import OrderRepository
            async with AsyncSessionLocal() as session:
                repo = OrderRepository(session)
                order = await repo.get_by_funpay_id(order_id)
            if order:
                await self._notifier.notify_dispute(
                    order_id=order_id,
                    buyer=order.buyer_username,
                    chat_id=order.chat_id,
                )

        # Cancelled or completed → close any active rental
        if new_status in ("cancelled", "completed"):
            from db.database import AsyncSessionLocal
            from db.models import RentalEndReason
            from db.repositories.order_repo import OrderRepository
            from db.repositories.rental_repo import RentalRepository
            async with AsyncSessionLocal() as session:
                order_repo = OrderRepository(session)
                rental_repo = RentalRepository(session)
                order = await order_repo.get_by_funpay_id(order_id)
                if order and order.account_id:
                    active = await rental_repo.get_active_by_account(order.account_id)
                    if active:
                        await rental_repo.close(active.id, RentalEndReason.ERROR)


LOCK_FILE = Path(__file__).parent / "bot.lock"


def acquire_lock() -> None:
    """Prevent running two instances of the bot at once (avoids Telegram getUpdates conflict)."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None

        # Check if the previous PID is actually still running
        if old_pid and _pid_alive(old_pid):
            print(
                f"\n[ERROR] Another bot instance is already running (PID={old_pid}).\n"
                "Close that window first, or kill the process via Task Manager.\n"
                f"If you're sure no other instance is running, delete the file:\n"
                f"  {LOCK_FILE}\n"
            )
            sys.exit(1)
        else:
            logger.warning("Found stale lock file (PID=%s not running) — removing", old_pid)

    LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return str(pid) in out
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


async def main() -> None:
    logger.info("Starting Valorant FunPay Bot")

    await init_db()
    logger.info("Database initialized")

    event_queue: asyncio.Queue = asyncio.Queue()

    async with FunPayClient() as funpay_client:
        logger.info("FunPay client connected")

        bot, dp = await create_bot()

        # Drop webhook + pending updates to avoid conflict with stale sessions
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Telegram webhook cleared, pending updates dropped")
        except Exception as e:
            logger.warning("Could not clear Telegram webhook: %s", e)

        notifier = NotificationService(bot)
        sender = FunPaySender()
        delivery = RentalDeliveryService(funpay_client, sender, notifier)
        qr_pipeline = QrPipeline(funpay_client, sender, notifier)
        poller = FunPayPoller(funpay_client, event_queue)
        dispatcher = EventDispatcher(event_queue, funpay_client, notifier, delivery, qr_pipeline)
        scheduler = RentalScheduler(funpay_client, sender, notifier)

        await asyncio.gather(
            poller.run(),
            dispatcher.run(),
            scheduler.run(),
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        )


if __name__ == "__main__":
    acquire_lock()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        _release_lock()
