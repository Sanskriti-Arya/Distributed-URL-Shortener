import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from . import config
from .analytics import enqueue_click
from .codegen import code_from_id
from .ratelimit import allow
from .validation import CODE_RE, ValidationError, validate_alias, validate_url

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

NEGATIVE = "__none__"
MAX_EXPIRY_SECONDS = 10 * 365 * 24 * 3600
STATS = {k: 0 for k in ("shorten", "redirect", "cache_hit", "cache_miss", "not_found", "expired",
                        "rate_limited", "db_errors")}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
    app.state.cache = aioredis.from_url(config.CACHE_REDIS_URL, decode_responses=True)
    app.state.queue = aioredis.from_url(config.QUEUE_REDIS_URL, decode_responses=True)
    yield
    await app.state.db.close()
    await app.state.cache.aclose()
    await app.state.queue.aclose()


app = FastAPI(title="URL Shortener", lifespan=lifespan)


@app.middleware("http")
async def served_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = config.INSTANCE_ID  # makes load balancing visible
    return response


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class ShortenRequest(BaseModel):
    url: str
    custom_alias: Optional[str] = None
    expires_in_seconds: Optional[int] = Field(default=None, gt=0, le=MAX_EXPIRY_SECONDS)


@app.post("/shorten", status_code=201)
async def shorten(body: ShortenRequest, request: Request):
    ok, retry_after = await allow(request.app.state.cache, f"shorten:{client_ip(request)}",
                                  config.RATE_LIMIT_PER_MIN, config.RATE_WINDOW_SECONDS)
    if not ok:
        STATS["rate_limited"] += 1
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry_after)})
    try:
        url = validate_url(body.url)
        alias = validate_alias(body.custom_alias) if body.custom_alias else None
    except ValidationError as e:
        raise HTTPException(422, str(e))

    expires_at = None
    if body.expires_in_seconds:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in_seconds)

    try:
        async with request.app.state.db.acquire() as conn:
            new_id = await conn.fetchval("SELECT nextval('url_id_seq')")
            code = alias or code_from_id(new_id)
            await conn.execute(
                "INSERT INTO urls (id, code, long_url, is_custom, expires_at) VALUES ($1,$2,$3,$4,$5)",
                new_id, code, url, alias is not None, expires_at)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "alias already taken")
    except (asyncpg.PostgresError, OSError):
        STATS["db_errors"] += 1
        raise HTTPException(503, "database unavailable")

    STATS["shorten"] += 1
    return {"short_url": f"{config.BASE_URL}/{code}", "code": code,
            "expires_at": expires_at.isoformat() if expires_at else None}


@app.get("/health")
async def health(request: Request):
    status = {"instance": config.INSTANCE_ID}
    try:
        await request.app.state.db.fetchval("SELECT 1")
        status["postgres"] = "ok"
    except Exception:  # noqa: BLE001
        status["postgres"] = "down"
    try:
        await request.app.state.cache.ping()
        status["cache"] = "ok"
    except Exception:  # noqa: BLE001
        status["cache"] = "down"
    # Cache being down degrades us; Postgres being down breaks us.
    code = 200 if status["postgres"] == "ok" else 503
    return JSONResponse(status, status_code=code)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return "".join(f'shortener_{k}_total{{instance="{config.INSTANCE_ID}"}} {v}\n' for k, v in STATS.items())


@app.get("/analytics/{code}")
async def analytics(code: str, request: Request):
    db = request.app.state.db
    try:
        if not await db.fetchval("SELECT 1 FROM urls WHERE code=$1", code):
            raise HTTPException(404, "unknown short code")
        totals = await db.fetchrow(
            "SELECT count(*) AS clicks, count(DISTINCT visitor_id) AS uniq FROM clicks WHERE code=$1", code)
        top_country = await db.fetchval(
            "SELECT country FROM clicks WHERE code=$1 GROUP BY country ORDER BY count(*) DESC LIMIT 1", code)
        top_ref = await db.fetchval(
            "SELECT referrer FROM clicks WHERE code=$1 GROUP BY referrer ORDER BY count(*) DESC LIMIT 1", code)
    except (asyncpg.PostgresError, OSError):
        STATS["db_errors"] += 1
        raise HTTPException(503, "database unavailable")
    return {"clicks": totals["clicks"], "unique_users": totals["uniq"],
            "top_country": top_country, "top_referrer": top_ref}


@app.get("/{code}")
async def redirect(code: str, request: Request, background: BackgroundTasks):
    if not CODE_RE.match(code):
        raise HTTPException(404, "not found")
    cache = request.app.state.cache
    key = f"url:{code}"

    cached = None
    try:
        cached = await cache.get(key)
    except RedisError:
        log.warning("cache unavailable, falling back to Postgres")  # degrade, don't fail

    if cached == NEGATIVE:
        STATS["not_found"] += 1
        raise HTTPException(404, "not found")

    if cached:
        STATS["cache_hit"] += 1
        long_url = cached
    else:
        STATS["cache_miss"] += 1
        try:
            row = await request.app.state.db.fetchrow(
                "SELECT long_url, expires_at FROM urls WHERE code=$1", code)
        except (asyncpg.PostgresError, OSError):
            STATS["db_errors"] += 1
            raise HTTPException(503, "database unavailable")

        if row is None:
            STATS["not_found"] += 1
            try:
                await cache.set(key, NEGATIVE, ex=config.NEGATIVE_TTL_SECONDS)  # blunt scans / hot misses
            except RedisError:
                pass
            raise HTTPException(404, "not found")

        ttl = config.CACHE_TTL_SECONDS
        if row["expires_at"] is not None:
            remaining = int((row["expires_at"] - datetime.now(timezone.utc)).total_seconds())
            if remaining <= 0:
                STATS["expired"] += 1
                raise HTTPException(410, "link expired")
            ttl = min(ttl, remaining)  # cache entry can never outlive the link
        long_url = row["long_url"]
        try:
            await cache.set(key, long_url, ex=ttl)
        except RedisError:
            pass

    STATS["redirect"] += 1
    country = request.headers.get("cf-ipcountry") or request.headers.get("x-country") or "XX"
    background.add_task(enqueue_click, request.app.state.queue, code, client_ip(request),
                        request.headers.get("user-agent", ""), country, request.headers.get("referer"))
    # 302, not 301: browsers cache 301s forever, so we'd never see repeat clicks and couldn't expire links.
    return RedirectResponse(long_url, status_code=302)
