from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin


class OrderStatus(str, Enum):
    PAID = "paid"
    ACTIVE = "active"
    COMPLETED = "completed"
    DISPUTE = "dispute"
    CANCELLED = "cancelled"


class AccountState(str, Enum):
    FREE = "free"            # not logged in, awaiting setup
    READY = "ready"          # bot logged in, session saved, ready to rent
    RENTED = "rented"        # currently rented to a buyer
    LOCKED = "locked"        # error state — needs manual intervention
    BANNED = "banned"        # Riot ban detected — excluded from rentals
    MFA_REQUIRED = "mfa_required"  # waiting for admin to enter MFA code


class RentalEndReason(str, Enum):
    TIME_EXPIRED = "time_expired"
    MANUAL_KICK = "manual_kick"
    GLOBAL_KICK = "global_kick"
    ERROR = "error"


class ValorantAccount(Base, TimestampMixin):
    __tablename__ = "valorant_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    riot_username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    riot_password: Mapped[str] = mapped_column(String(200), nullable=False)
    rank_display: Mapped[str] = mapped_column(String(50), default="Unranked")

    state: Mapped[str] = mapped_column(
        String(20), default=AccountState.FREE, nullable=False
    )
    storage_state_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Legacy Phase 1 field — kept for backward compat, no longer written
    riot_cookies: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- Phase 2 RSO tokens (replaces cookies) ---
    rso_ssid: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rso_puuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    rso_region: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    rso_entitlement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rso_access_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Ban tracking
    banned_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ban_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Email mailbox for 2FA codes from Riot. firstmail.ltd-style mailboxes
    # are common (1 mailbox = 1 Riot account).
    email_address: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    email_password: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    email_imap_host: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email_imap_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    rentals: Mapped[list[RentalSession]] = relationship(
        "RentalSession", back_populates="account"
    )


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funpay_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    buyer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    buyer_username: Mapped[str] = mapped_column(String(100), nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("valorant_accounts.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.PAID, nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="RUB")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rental_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auto_replied: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class RentalSession(Base):
    __tablename__ = "rental_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("valorant_accounts.id"), nullable=False
    )
    buyer_funpay_id: Mapped[int] = mapped_column(Integer, nullable=False)
    buyer_username: Mapped[str] = mapped_column(String(100), nullable=False)
    order_funpay_id: Mapped[str] = mapped_column(String(50), nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_reason: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    account: Mapped[ValorantAccount] = relationship(
        "ValorantAccount", back_populates="rentals"
    )


class Buyer(Base, TimestampMixin):
    """Aggregate stats per buyer."""
    __tablename__ = "buyers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funpay_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)

    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_minutes_rented: Mapped[int] = mapped_column(Integer, default=0)
    total_spent: Mapped[float] = mapped_column(Float, default=0.0)
    last_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SeenMessage(Base):
    __tablename__ = "seen_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    funpay_msg_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
