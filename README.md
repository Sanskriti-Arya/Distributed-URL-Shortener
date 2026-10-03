# Distributed URL Shortener

FastAPI + PostgreSQL + Redis, behind an nginx load balancer with three stateless API nodes and an async analytics pipeline (Redis Streams + worker). Full reasoning and capacity math are in [DESIGN.md](DESIGN.md).

## Run

```bash
docker compose up -d --build
curl -s localhost:8000/health
```

## Try it

```bash
# shorten (optional custom_alias, expires_in_seconds)
curl -s -X POST localhost:8000/shorten -H 'content-type: application/json' \
  -d '{"url": "https://example.com/very/long/url"}'

# redirect (302)
curl -i localhost:8000/<code>

# analytics (eventually consistent, a few seconds behind)
curl -s localhost:8000/analytics/<code>

# per-node counters (see X-Served-By to watch load balancing)
curl -s localhost:8000/metrics
```

## Test

```bash
pip install -r requirements-dev.txt
pytest tests/test_codegen.py tests/test_validation.py   # unit, no services needed
pytest tests/test_integration.py                        # needs the stack running
```

## Benchmark

```bash
RATE_LIMIT_PER_MIN=1000000 docker compose up -d --build
python scripts/benchmark.py --urls 2000 --requests 20000 --concurrency 100
```

Record the results in the table at the bottom of DESIGN.md.

## Failure drills (good for interview stories)

```bash
docker compose stop api2          # nginx routes around it
docker compose stop redis-cache   # redirects still work, served from Postgres
docker compose stop worker        # redirects work; stream backs up; restart and watch it drain
docker compose stop redis-queue   # redirects work; click events are dropped and logged
docker compose exec redis-queue redis-cli XLEN clicks
```

## Layout

```
app/        main.py (API) worker.py (analytics consumer) codegen.py ratelimit.py validation.py analytics.py
db/         schema.sql
nginx/      nginx.conf
tests/      unit + integration
scripts/    benchmark.py
```

## Resume bullet (use only after you have benchmarked it)

> **Distributed URL Shortener** | Python, FastAPI, PostgreSQL, Redis, Docker, nginx
> - Designed a horizontally scalable URL-shortening service: stateless API nodes behind a load balancer, cache-aside Redis, PostgreSQL persistence, with capacity estimates for 100M redirects/day.
> - Built asynchronous click analytics with Redis Streams and an idempotent worker, plus rate limiting and graceful degradation when cache or queue fail. Benchmarked at <your measured req/s, p99>.
