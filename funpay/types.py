from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FPMessage:
    id: int
    chat_id: int
    chat_name: str
    text: str
    author_id: int
    is_incoming: bool
    timestamp: datetime
    # URLs of image attachments. Filled by parser when <a class="chat-img-link">
    # / <img class="chat-img">/ etc. are present.
    image_urls: list[str] = field(default_factory=list)


@dataclass
class FPOrder:
    id: str
    buyer_id: int
    buyer_username: str
    chat_id: int
    status: str
    description: str = ""
    price: float = 0.0
    currency: str = "RUB"


@dataclass
class FPChat:
    id: int
    name: str
    last_message_id: int
    unread: bool


@dataclass
class FPEvent:
    type: str
    payload: dict = field(default_factory=dict)
