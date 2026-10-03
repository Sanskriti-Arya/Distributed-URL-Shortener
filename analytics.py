import logging
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlparse

from . import config

log = logging.getLogger("analytics")


def referrer_host(referrer: str | None) -> str:
    if not referrer:
        return "direct"
    return urlparse(referrer).hostname or "direct"


async def enqueue_click(queue, code: str, ip: str, user_agent: str, country: str, referrer: str | None) -> bool:
    """Publish a click event to a Redis Stream. Never raises: analytics must not break redirects."""
    event = {
        "event_id": str(uuid.uuid4()),
        "code": code,
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "ua": user_agent[:256],
        "country": country,
        "referrer": referrer_host(referrer),
        "visitor": sha256(f"{ip}|{user_agent}".encode()).hexdigest()[:16],
    }
    try:
        await queue.xadd(config.CLICK_STREAM, event, maxlen=config.CLICK_STREAM_MAXLEN, approximate=True)
        return True
    except Exception:  # noqa: BLE001 - deliberate: drop the event, keep serving
        log.exception("failed to enqueue click for %s", code)
        return False
