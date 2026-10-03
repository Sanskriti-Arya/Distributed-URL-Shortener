import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shortener:shortener@localhost:5432/shortener")
# Two Redis roles on purpose: the cache may evict (allkeys-lru); the queue must never evict.
CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "redis://localhost:6379/0")
QUEUE_REDIS_URL = os.getenv("QUEUE_REDIS_URL", "redis://localhost:6380/0")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
INSTANCE_ID = os.getenv("INSTANCE_ID", "local")

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
NEGATIVE_TTL_SECONDS = int(os.getenv("NEGATIVE_TTL_SECONDS", "60"))

RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
RATE_WINDOW_SECONDS = 60

CLICK_STREAM = "clicks"
CLICK_GROUP = "analytics"
CLICK_STREAM_MAXLEN = int(os.getenv("CLICK_STREAM_MAXLEN", "1000000"))
