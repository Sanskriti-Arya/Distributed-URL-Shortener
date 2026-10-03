import time

from redis.exceptions import RedisError


async def allow(redis, key: str, limit: int, window: int) -> tuple[bool, int]:
    """Fixed-window counter in Redis. Returns (allowed, retry_after_seconds).

    Fails OPEN: if Redis is unreachable we would rather serve traffic than reject everyone.
    Known weakness of fixed windows: up to 2x burst at a window boundary (see DESIGN.md).
    """
    now = int(time.time())
    bucket = now // window
    k = f"rl:{key}:{bucket}"
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(k)
        pipe.expire(k, window)
        count, _ = await pipe.execute()
    except RedisError:
        return True, 0
    if count > limit:
        return False, window - (now % window)
    return True, 0
