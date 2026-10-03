"""Analytics worker: Redis Stream (consumer group) -> batched inserts into Postgres.

Delivery is at-least-once: we XACK only after the batch is committed. Duplicates (redelivery after
a crash) are absorbed by the PRIMARY KEY on event_id + ON CONFLICT DO NOTHING, so the net effect is
exactly-once in the clicks table. Run any number of workers; the group splits the stream between them.
"""
import asyncio
import logging
import os
import socket
import time
from datetime import datetime

import asyncpg
import redis.asyncio as aioredis
from redis.exceptions import RedisError, ResponseError

from . import config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")
CONSUMER = os.getenv("HOSTNAME", socket.gethostname())
BATCH = 200
RECLAIM_EVERY = 30        # seconds
RECLAIM_IDLE_MS = 60_000  # messages unacked this long belong to a dead consumer


def to_row(fields: dict):
    return (fields["event_id"], fields["code"], datetime.fromisoformat(fields["ts"]), fields.get("ip"),
            fields.get("ua"), fields.get("country"), fields.get("referrer"), fields.get("visitor"))


async def flush(pool, queue, entries) -> None:
    rows, ack_ids = [], []
    for msg_id, fields in entries:
        ack_ids.append(msg_id)
        try:
            rows.append(to_row(fields))
        except (KeyError, ValueError):
            log.error("dropping malformed event %s: %r", msg_id, fields)  # poison message: ack, don't loop
    if rows:
        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO clicks (event_id, code, ts, ip, user_agent, country, referrer, visitor_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (event_id) DO NOTHING", rows)
    await queue.xack(config.CLICK_STREAM, config.CLICK_GROUP, *ack_ids)
    log.info("flushed %d events", len(rows))


async def main() -> None:
    pool = None
    while pool is None:
        try:
            pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=3)
        except (OSError, asyncpg.PostgresError):
            log.warning("postgres not ready, retrying")
            await asyncio.sleep(2)
    queue = aioredis.from_url(config.QUEUE_REDIS_URL, decode_responses=True)
    try:
        await queue.xgroup_create(config.CLICK_STREAM, config.CLICK_GROUP, id="0", mkstream=True)
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    last_reclaim = 0.0
    log.info("worker %s started", CONSUMER)
    while True:
        try:
            if time.time() - last_reclaim > RECLAIM_EVERY:
                last_reclaim = time.time()
                res = await queue.xautoclaim(config.CLICK_STREAM, config.CLICK_GROUP, CONSUMER,
                                             min_idle_time=RECLAIM_IDLE_MS, start_id="0-0", count=BATCH)
                if res[1]:
                    log.info("reclaimed %d stale events", len(res[1]))
                    await flush(pool, queue, res[1])

            resp = await queue.xreadgroup(config.CLICK_GROUP, CONSUMER, {config.CLICK_STREAM: ">"},
                                          count=BATCH, block=5000)
            for _stream, entries in resp or []:
                await flush(pool, queue, entries)  # on failure: not acked -> redelivered later
        except (RedisError, asyncpg.PostgresError, OSError):
            log.exception("worker loop error; backing off")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
