import os
from typing import Optional

import asyncpg


class Database:
    """Async PostgreSQL access layer using asyncpg connection pooling."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Create a connection pool."""
        dsn = os.getenv("POSTGRES_DSN")
        if not dsn:
            raise RuntimeError(
                "POSTGRES_DSN env var is required (e.g. postgresql://user:pass@host:port/db)"
            )
        self._pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)

    async def disconnect(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        """Return the active pool or raise."""
        if self._pool is None:
            raise RuntimeError("Database pool not initialized")
        return self._pool


db = Database()
