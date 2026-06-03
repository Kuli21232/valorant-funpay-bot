from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AccountState, ValorantAccount


class AccountRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_all(self) -> list[ValorantAccount]:
        result = await self._session.execute(select(ValorantAccount))
        return list(result.scalars().all())

    async def get_by_id(self, account_id: int) -> Optional[ValorantAccount]:
        return await self._session.get(ValorantAccount, account_id)

    async def get_by_username(self, username: str) -> Optional[ValorantAccount]:
        result = await self._session.execute(
            select(ValorantAccount).where(ValorantAccount.riot_username == username)
        )
        return result.scalar_one_or_none()

    async def get_by_state(self, state: AccountState) -> list[ValorantAccount]:
        result = await self._session.execute(
            select(ValorantAccount).where(ValorantAccount.state == state)
        )
        return list(result.scalars().all())

    async def create(
        self,
        riot_username: str,
        riot_password: str,
        rank_display: str = "Unranked",
        notes: Optional[str] = None,
    ) -> ValorantAccount:
        account = ValorantAccount(
            riot_username=riot_username,
            riot_password=riot_password,
            rank_display=rank_display,
            notes=notes,
            state=AccountState.FREE,
        )
        self._session.add(account)
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def set_state(
        self,
        account_id: int,
        state: AccountState,
        storage_state_path: Optional[str] = None,
    ) -> Optional[ValorantAccount]:
        account = await self.get_by_id(account_id)
        if account:
            account.state = state
            if state == AccountState.READY:
                account.last_login_at = datetime.utcnow()
            if storage_state_path is not None:
                account.storage_state_path = storage_state_path
            await self._session.commit()
            await self._session.refresh(account)
        return account

    async def update_rank(self, account_id: int, rank: str) -> Optional[ValorantAccount]:
        account = await self.get_by_id(account_id)
        if account:
            account.rank_display = rank
            await self._session.commit()
            await self._session.refresh(account)
        return account

    async def delete(self, account_id: int) -> bool:
        account = await self.get_by_id(account_id)
        if account:
            await self._session.delete(account)
            await self._session.commit()
            return True
        return False

    async def save_tokens(self, account_id: int, tokens) -> Optional[ValorantAccount]:
        """Persist RSO tokens (ssid, puuid, region, entitlement, expires_at)."""
        account = await self.get_by_id(account_id)
        if not account:
            return None
        account.rso_ssid = tokens.ssid
        account.rso_puuid = tokens.puuid
        account.rso_region = tokens.region
        account.rso_entitlement = tokens.entitlement_token
        account.rso_access_expires_at = tokens.expires_at
        account.last_login_at = datetime.utcnow()
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def update_password(self, account_id: int, new_password: str) -> Optional[ValorantAccount]:
        account = await self.get_by_id(account_id)
        if not account:
            return None
        account.riot_password = new_password
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def mark_banned(self, account_id: int, until=None, reason: str = "") -> Optional[ValorantAccount]:
        account = await self.get_by_id(account_id)
        if not account:
            return None
        account.state = AccountState.BANNED
        account.banned_until = until
        account.ban_reason = reason[:200] if reason else None
        await self._session.commit()
        await self._session.refresh(account)
        return account

    async def get_first_ready(self) -> Optional[ValorantAccount]:
        """Find an account ready to rent. Excludes BANNED accounts and any
        whose ban has just expired (auto-unban handled by scheduler)."""
        result = await self._session.execute(
            select(ValorantAccount).where(
                ValorantAccount.state == AccountState.READY
            ).limit(1)
        )
        return result.scalar_one_or_none()
