import json
import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from funpay.types import FPChat, FPMessage, FPOrder


def extract_csrf_token(html: str) -> Optional[str]:
    """Extract CSRF token from FunPay HTML. Tries multiple known formats."""
    soup = BeautifulSoup(html, "html.parser")

    # 1. Modern FunPay: <body data-app-data='{"csrf-token":"...", "userId":N, ...}'>
    body = soup.find("body")
    if body and body.has_attr("data-app-data"):
        try:
            data = json.loads(body["data-app-data"])
            token = data.get("csrf-token") or data.get("csrfToken")
            if token:
                return token
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Legacy: <meta name="csrf-token" content="...">
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]

    # 3. Inline script: var csrfToken = "..."
    for script in soup.find_all("script"):
        text = script.string or ""
        m = re.search(r'["\']csrf[-_]?token["\']\s*:\s*["\']([a-f0-9]{20,})["\']', text, re.I)
        if m:
            return m.group(1)
        m = re.search(r'csrfToken\s*=\s*["\']([a-f0-9]{20,})["\']', text, re.I)
        if m:
            return m.group(1)

    # 4. Raw regex over whole HTML as last resort
    m = re.search(r'data-app-data=[\'"]([^\'"]+)[\'"]', html)
    if m:
        try:
            data = json.loads(m.group(1).replace("&quot;", '"'))
            token = data.get("csrf-token") or data.get("csrfToken")
            if token:
                return token
        except (json.JSONDecodeError, TypeError):
            pass

    return None


def extract_user_id(html: str) -> Optional[int]:
    """Extract logged-in user ID from FunPay HTML data-app-data."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if body and body.has_attr("data-app-data"):
        try:
            data = json.loads(body["data-app-data"])
            uid = data.get("userId") or data.get("user-id")
            if uid:
                return int(uid)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return None


def parse_chat_bookmarks(data: list[dict]) -> list[FPChat]:
    chats: list[FPChat] = []
    for item in data:
        try:
            chats.append(
                FPChat(
                    id=int(item.get("id", 0)),
                    name=item.get("title", ""),
                    last_message_id=int(item.get("lastMessage", {}).get("id", 0)),
                    unread=bool(item.get("unread", False)),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return chats


def parse_messages_from_html(html: str, chat_id: int, my_user_id: int) -> list[FPMessage]:
    """Parse messages from FunPay chat history HTML."""
    soup = BeautifulSoup(html, "html.parser")
    messages: list[FPMessage] = []

    for msg_div in soup.select(".message-item"):
        try:
            msg_id_str = msg_div.get("data-id", "")
            if not msg_id_str:
                continue
            msg_id = int(msg_id_str)

            author_id_str = msg_div.get("data-author", "")
            author_id = int(author_id_str) if author_id_str else 0
            is_incoming = author_id != my_user_id

            text_el = msg_div.select_one(".message-text")
            text = text_el.get_text(strip=True) if text_el else ""

            name_el = msg_div.select_one(".media-user-name")
            chat_name = name_el.get_text(strip=True) if name_el else ""

            # Image attachments — FunPay wraps uploaded images in
            # <a class="chat-img-link" href="full-url"><img src="thumb" /></a>
            image_urls: list[str] = []
            for a in msg_div.select("a.chat-img-link[href]"):
                href = a.get("href", "").strip()
                if href:
                    image_urls.append(href)
            # Some renderings have plain <img class="chat-img">
            for img in msg_div.select("img.chat-img[src]"):
                src = img.get("src", "").strip()
                if src and src not in image_urls:
                    image_urls.append(src)

            messages.append(
                FPMessage(
                    id=msg_id,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    text=text,
                    author_id=author_id,
                    is_incoming=is_incoming,
                    timestamp=datetime.utcnow(),
                    image_urls=image_urls,
                )
            )
        except (ValueError, AttributeError):
            continue

    return messages


def parse_orders_from_html(html: str) -> list[FPOrder]:
    """Parse orders from FunPay orders page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    orders: list[FPOrder] = []

    for row in soup.select("a.tc-item"):
        try:
            href = row.get("href", "")
            order_id_match = re.search(r"/orders/([^/]+)/", href)
            if not order_id_match:
                continue
            order_id = order_id_match.group(1)

            buyer_el = row.select_one(".media-user-name")
            buyer_name = buyer_el.get_text(strip=True) if buyer_el else "Unknown"

            status_el = row.select_one(".tc-status")
            status_text = status_el.get_text(strip=True).lower() if status_el else ""
            status = _map_order_status(status_text)

            desc_el = row.select_one(".tc-desc-text")
            description = desc_el.get_text(strip=True) if desc_el else ""

            price_el = row.select_one(".tc-price")
            price_text = price_el.get_text(strip=True) if price_el else "0"
            price = _parse_price(price_text)

            orders.append(
                FPOrder(
                    id=order_id,
                    buyer_id=0,
                    buyer_username=buyer_name,
                    chat_id=0,
                    status=status,
                    description=description,
                    price=price,
                )
            )
        except (AttributeError, ValueError):
            continue

    return orders


def _map_order_status(text: str) -> str:
    if "оплач" in text or "paid" in text:
        return "paid"
    if "закрыт" in text or "completed" in text or "выполнен" in text:
        return "completed"
    if "спор" in text or "dispute" in text:
        return "dispute"
    if "отмен" in text or "cancel" in text:
        return "cancelled"
    return "paid"


def _parse_price(text: str) -> float:
    digits = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return 0.0
