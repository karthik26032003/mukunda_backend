"""
helpers/db.py
─────────────
asyncpg connection pool + all database operations for the batch call queue.

Tables:
  batches       — one row per Excel upload / batch job
  mukunda_calls — one row per phone number in a batch
"""

import logging
import os

import asyncpg

logger = logging.getLogger("db")

# Module-level pool — initialised once on startup
_pool: asyncpg.Pool | None = None

def get_concurrency() -> int:
    """Max simultaneous active calls per batch. Set BATCH_CONCURRENCY in .env to override."""
    return int(os.getenv("BATCH_CONCURRENCY", "3"))


# ── Pool lifecycle ────────────────────────────────────────────────────────────

async def init_pool() -> None:
    global _pool
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        logger.warning("DATABASE_URL not set — DB features disabled")
        return
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
    logger.info("DB pool initialised")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        logger.info("DB pool closed")


def get_pool() -> asyncpg.Pool | None:
    return _pool


# ── Table creation ────────────────────────────────────────────────────────────

CREATE_BATCHES = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT        PRIMARY KEY,
    agent_id    TEXT        NOT NULL,
    from_number TEXT        NOT NULL,
    total       INT         NOT NULL DEFAULT 0,
    queued      INT         NOT NULL DEFAULT 0,
    active      INT         NOT NULL DEFAULT 0,
    succeeded   INT         NOT NULL DEFAULT 0,
    failed      INT         NOT NULL DEFAULT 0,
    status      TEXT        NOT NULL DEFAULT 'running',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

CREATE_MUKUNDA_CALLS = """
CREATE TABLE IF NOT EXISTS mukunda_calls (
    id           BIGSERIAL   PRIMARY KEY,
    batch_id     TEXT        NOT NULL REFERENCES batches(batch_id),
    phone_number TEXT        NOT NULL,
    call_id      TEXT,
    status       TEXT        NOT NULL DEFAULT 'queued',
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mukunda_calls_batch_id ON mukunda_calls(batch_id);
CREATE INDEX IF NOT EXISTS idx_mukunda_calls_call_id  ON mukunda_calls(call_id);
CREATE INDEX IF NOT EXISTS idx_mukunda_calls_status   ON mukunda_calls(batch_id, status);
"""


async def create_tables() -> None:
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute(CREATE_BATCHES)
        await conn.execute(CREATE_MUKUNDA_CALLS)
    logger.info("DB tables ready")


# ── Batch operations ──────────────────────────────────────────────────────────

async def create_batch(
    batch_id: str,
    agent_id: str,
    from_number: str,
    total: int,
) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        INSERT INTO batches (batch_id, agent_id, from_number, total, queued, status)
        VALUES ($1, $2, $3, $4, $4, 'running')
        """,
        batch_id, agent_id, from_number, total,
    )


async def insert_batch_calls(batch_id: str, phone_numbers: list[str]) -> None:
    """Bulk insert all numbers as 'queued'."""
    pool = get_pool()
    if not pool:
        return
    rows = [(batch_id, num) for num in phone_numbers]
    await pool.executemany(
        "INSERT INTO mukunda_calls (batch_id, phone_number) VALUES ($1, $2)",
        rows,
    )


async def pop_next_queued(batch_id: str) -> str | None:
    """
    Atomically grab the next queued number and mark it 'initiated'.
    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent webhook calls
    never double-pick the same row.
    Returns the phone_number or None if queue is empty.
    """
    pool = get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE mukunda_calls
            SET    status     = 'initiated',
                   updated_at = NOW()
            WHERE  id = (
                SELECT id FROM mukunda_calls
                WHERE  batch_id = $1 AND status = 'queued'
                ORDER  BY id ASC
                LIMIT  1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, phone_number
            """,
            batch_id,
        )
        if row:
            # Increment active, decrement queued on the batch
            await conn.execute(
                """
                UPDATE batches
                SET    active     = active + 1,
                       queued     = queued - 1,
                       updated_at = NOW()
                WHERE  batch_id = $1
                """,
                batch_id,
            )
            return row["phone_number"]
        return None


async def update_call_status_by_phone(
    batch_id: str,
    phone_number: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update call status by phone when call_id isn't available yet (startup failure)."""
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE mukunda_calls
        SET    status     = $1,
               error      = $2,
               updated_at = NOW()
        WHERE  batch_id     = $3
          AND  phone_number = $4
          AND  status       = 'initiated'
        """,
        status, error, batch_id, phone_number,
    )


async def set_call_id(batch_id: str, phone_number: str, call_id: str) -> None:
    """Store the Ultravox callId once the call is successfully created."""
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE mukunda_calls
        SET    call_id    = $1,
               updated_at = NOW()
        WHERE  batch_id     = $2
          AND  phone_number = $3
          AND  status       = 'initiated'
        """,
        call_id, batch_id, phone_number,
    )


async def update_call_status(
    call_id: str,
    status: str,           # joined | ended | no_answer | failed
    error: str | None = None,
) -> str | None:
    """
    Update mukunda_calls row by call_id.
    Returns batch_id so the caller can decide whether to pop the next number.
    """
    pool = get_pool()
    if not pool:
        return None
    row = await pool.fetchrow(
        """
        UPDATE mukunda_calls
        SET    status     = $1,
               error      = $2,
               updated_at = NOW()
        WHERE  call_id = $3
        RETURNING batch_id
        """,
        status, error, call_id,
    )
    return row["batch_id"] if row else None


async def close_call_on_batch(
    batch_id: str,
    succeeded: bool,
) -> dict:
    """
    Called when a call fully ends (ended / failed / no_answer).
    Decrements active, increments succeeded or failed.
    Returns updated batch row.
    """
    pool = get_pool()
    if not pool:
        return {}
    col = "succeeded" if succeeded else "failed"
    row = await pool.fetchrow(
        f"""
        UPDATE batches
        SET    active     = active - 1,
               {col}     = {col} + 1,
               updated_at = NOW()
        WHERE  batch_id = $1
        RETURNING *
        """,
        batch_id,
    )
    return dict(row) if row else {}


async def mark_batch_complete(batch_id: str) -> None:
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE batches
        SET    status     = 'completed',
               updated_at = NOW()
        WHERE  batch_id = $1
        """,
        batch_id,
    )


async def get_batch(batch_id: str) -> dict | None:
    pool = get_pool()
    if not pool:
        return None
    row = await pool.fetchrow(
        "SELECT * FROM batches WHERE batch_id = $1",
        batch_id,
    )
    return dict(row) if row else None


async def mark_failed_initiated_calls() -> None:
    """
    On startup: any calls left as 'initiated' from a crashed previous run
    should be reset to 'failed' so the queue doesn't stall.
    """
    pool = get_pool()
    if not pool:
        return
    await pool.execute(
        """
        UPDATE mukunda_calls
        SET    status = 'failed', error = 'server restart', updated_at = NOW()
        WHERE  batch_id IN (SELECT batch_id FROM batches WHERE status = 'running')
          AND  status = 'initiated'
        """,
    )
