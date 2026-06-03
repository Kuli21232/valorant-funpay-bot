import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from db.base import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Create tables, then add any columns that exist in the models but not
    in the existing SQLite file. Avoids forcing users to delete bot.db on
    every schema change."""
    # Import models so they register with Base.metadata before create_all/migrate
    import db.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migration pass — uses a separate connection for clarity
    async with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            res = await conn.execute(text(f"PRAGMA table_info('{table_name}')"))
            existing_cols = {row[1] for row in res.fetchall()}
            if not existing_cols:
                continue  # table doesn't exist (shouldn't happen after create_all)
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                col_type = column.type.compile(dialect=conn.dialect)
                ddl = (f'ALTER TABLE "{table_name}" ADD COLUMN '
                       f'"{column.name}" {col_type}')
                logger.info("Migrating schema: %s", ddl)
                try:
                    await conn.execute(text(ddl))
                except Exception as e:
                    logger.error("Could not add %s.%s: %s",
                                 table_name, column.name, e)
