# Design: Distributed URL Shortener

## 1. Requirements

Build a TinyURL-style service: turn a long URL into a short link, redirect fast, and report click analytics.

**Scale assumptions (drive every number below):** 10M new URLs/day, 100M redirects/day, 5-year retention, 10:1 read:write ratio.

## 2. Functional requirements

- `POST /shorten` creates a short URL, with optional custom alias and expiry.
- `GET /{code}` redirects to the original URL.
- `GET /analytics/{code}` returns clicks, unique users, top country, top referrer.
- URL validation, alias validation, link expiration, per-client rate limiting on creation.

## 3. Non-functional requirements

| Property | Target | Why |
|---|---|---|
| Redirect latency | p99 < 50 ms (cache hit) | Redirect is on the user's critical path |
| Availability | Redirects survive loss of Redis cache, one API node, and the analytics pipeline | Reads dominate and matter most |
| Consistency | Strong for create/alias uniqueness; eventual for analytics | Two people must never own one alias; click counts can lag seconds |
| Durability | Created URLs never lost; a few click events may be lost in extreme failure | Links are the product, analytics is a by-product |
| Scalability | Stateless API tier scales horizontally | Add nodes behind the LB |

## 4. API design

```
POST /shorten   {"url": "...", "custom_alias": "optional", "expires_in_seconds": 3600}
  201 {"short_url": "http://host/aB91xQ2", "code": "aB91xQ2", "expires_at": null}
  409 alias taken | 422 invalid input | 429 rate limited (Retry-After) | 503 DB down

GET /{code}            302 + Location | 404 unknown | 410 expired
GET /analytics/{code}  200 {"clicks","unique_users","top_country","top_referrer"} | 404
GET /health            200/503 (503 only if Postgres is down; cache down = degraded)
GET /metrics           Prometheus text counters, labelled per instance
```

**302, not 301.** Browsers cache 301 permanently, which would hide repeat clicks from analytics and make expiry/edits impossible. The cost is more redirect traffic, which the cache absorbs.

## 5. Architecture

```
Client -> nginx (least_conn, retries next node on failure)
            -> api1 / api2 / api3   (stateless FastAPI)
                 |-> redis-cache (allkeys-lru): url cache, negative cache, rate-limit counters
                 |-> PostgreSQL: source of truth for urls
                 '-> redis-queue (noeviction, AOF): click stream
                          '-> worker(s): batch insert -> clicks table in Postgres
```

Key decisions:

- **Stateless API nodes.** No local state, so any node serves any request and nodes are disposable.
- **Two Redis roles.** The cache is allowed to evict; the queue must never silently drop unread events. Putting both in one `allkeys-lru` instance would let memory pressure evict analytics events. Separate instances with different eviction policies avoid that.
- **Redis Streams instead of Kafka.** At ~1.2k events/s average, Streams with consumer groups give durable, replayable, at-least-once delivery with far less operational cost. Kafka becomes justified at much higher volume, multiple independent consumers, or long retention/replay needs (see Trade-offs).

## 6. Database schema

```sql
urls(id BIGINT PK, code TEXT UNIQUE, long_url TEXT, is_custom BOOL, created_at, expires_at NULL)
clicks(event_id UUID PK, code, ts, ip, user_agent, country, referrer, visitor_id)
INDEX clicks(code, ts)
```

- The `UNIQUE(code)` index is the only hot read path: point lookup by code.
- `clicks.event_id` as primary key makes the worker idempotent (section 10).
- Custom aliases live in the same `code` column, so one unique index guarantees global uniqueness across generated and custom codes.

## 7. URL generation algorithm

Options considered:

| Approach | Pros | Cons |
|---|---|---|
| Random 7-char code, retry on collision | Simple, unguessable | Collision probability grows with table size; needs a retry loop and an extra read/write |
| Hash of URL (MD5/SHA, truncate) | Same URL yields same code | Truncation collisions; needs handling; same URL cannot have different expiries/owners |
| **Sequence + base62 (chosen)** | Zero collisions by construction, one cheap `nextval`, compact | Sequence is a single-writer bottleneck; sequential codes are enumerable |

**Chosen:** `nextval` from a Postgres sequence, multiplied by a constant coprime to 62^7 (a bijection mod 62^7), then base62-encoded to 7 chars. This keeps uniqueness guaranteed while making consecutive ids look unrelated. It is obfuscation, not security: someone who knows the constant can reverse it. If codes must be truly unguessable, switch to random codes.

**Capacity:** 62^7 ≈ 3.5 x 10^12 codes. At 10M/day that is ~350,000 days, far beyond 5 years.

**Scaling the generator:** one sequence handles hundreds of writes/s easily. Beyond that, each API node reserves a block of ids (e.g. `nextval` in steps of 1000, using ids locally), or use per-shard sequences with the shard encoded in the id.

## 8. Caching strategy

Cache-aside on the redirect path:

```
GET /{code} -> redis HIT  -> redirect
            -> redis MISS -> Postgres -> populate redis (TTL) -> redirect
```

- **TTL:** 1 hour default, but `min(TTL, time until expiry)`, so a cached entry can never outlive its link.
- **Invalidation:** URLs are immutable once created, so there is no update-invalidate problem. Expiry is handled by TTL alignment. If edit/delete is added later, delete the key on write.
- **Negative caching:** unknown codes are cached for 60 s so scans and typos do not hammer Postgres.
- **Eviction:** `allkeys-lru` with a memory cap. Redirect traffic is heavily skewed (a small set of links gets most clicks), so LRU keeps the hot set resident.
- **Cache stampede:** when a hot key expires, many concurrent misses hit Postgres at once. Not mitigated in this version; fix is single-flight (a short `SET NX` lock so one request repopulates) or TTL jitter.
- **Redis down:** reads fall back to Postgres (higher latency, still correct).

## 9. Scaling strategy

Capacity estimates (assumptions flagged; replace with measured numbers from `scripts/benchmark.py`):

| Quantity | Calculation | Result |
|---|---|---|
| Writes/s (avg) | 10M / 86,400 | ~116/s |
| Reads/s (avg) | 100M / 86,400 | ~1,160/s |
| Peak (assume 5x avg) | | ~580 writes/s, ~5,800 reads/s |
| Row size (urls) | id 8 + code 7 + URL ~100 + timestamps 16 + overhead/index ~170 | ~300 B |
| URL storage | 10M x 365 x 5 yr = 18.25B rows x 300 B | ~5.5 TB |
| Cache size | 20% of daily redirects are distinct hot URLs: 20M entries x ~250 B | ~5 GB |
| DB read load | 90% cache hit => 10% miss | ~116/s avg, ~580/s peak |
| Redirect bandwidth | ~300 B response x 1,160/s | ~350 KB/s avg (~3 Mbps), ~15 Mbps peak |
| Click events | 100M/day x ~150 B | ~15 GB/day, ~5.5 TB/yr |
| Queue throughput | 1,160 events/s avg, 5,800/s peak; worker batches of 200 | ~6-30 batch inserts/s |

Interpretation:

- **API tier:** peak 5.8k redirects/s needs a handful of nodes. This repo runs 3; add more behind nginx. Verify per-node throughput with the benchmark rather than assuming.
- **Reads:** with a good hit ratio, one Postgres primary handles the miss traffic. Add read replicas (route cache-miss lookups there) when misses exceed what the primary can serve.
- **Writes:** ~580/s peak is within a single primary. Past that, shard.
- **Sharding (future):** shard `urls` by `hash(code)` using **consistent hashing**, so adding a shard moves only ~1/N of keys. Redirects know the code, so routing is direct. Custom aliases hash the same way, so uniqueness stays per-shard-correct.
- **Analytics storage is the real size problem** (5.5 TB/yr). Partition `clicks` by day, keep raw events for a limited window (e.g. 30-90 days), and roll up into per-code per-day aggregates, or move to a columnar store (ClickHouse). `GET /analytics` would then read rollups.
- **CAP:** Postgres primary with synchronous-style writes favors consistency over availability for creates (CP): during a partition from the primary, creates fail rather than risk duplicate aliases. Redirects are served from cache and replicas and favor availability (AP-leaning), accepting briefly stale data. Analytics is deliberately eventually consistent.
- **Replication:** streaming replication primary -> replicas; promote on failure. Replica lag means a just-created link might 404 if redirects read from a lagging replica; mitigate by reading the primary on cache miss for very new codes, or by writing through to the cache on create.

## 10. Failure scenarios

| Failure | Behavior | Mitigation |
|---|---|---|
| One API node dies | nginx `proxy_next_upstream` retries another node; `max_fails` ejects it | Stateless nodes, health checks |
| Cache Redis down | Redirects fall back to Postgres; rate limiter fails open | Higher latency, DB load rises; add cache replicas / Sentinel |
| Queue Redis down | Redirects still succeed; click events dropped (logged) | Accepts bounded analytics loss; AOF + replica to reduce it |
| Worker crashes mid-batch | Messages stay pending, not acked; `XAUTOCLAIM` re-delivers after 60 s | `event_id` primary key + `ON CONFLICT DO NOTHING` makes redelivery idempotent |
| Poison/malformed event | Logged, acked, dropped | Prevents an infinite retry loop |
| Postgres down | Creates and cache-miss redirects return 503; cached redirects keep working | Replica promotion, connection retry, `/health` returns 503 |
| Duplicate alias race | Unique index rejects the second insert; API returns 409 | DB enforces correctness, not application checks |
| Traffic spike / abuse on `/shorten` | 429 with `Retry-After` | Rate limiting per client IP |

**Idempotency / delivery semantics:** at-least-once delivery + idempotent consumer = effectively-once results in `clicks`.

**Rate limiting:** fixed-window counter in Redis (`INCR` + `EXPIRE`) keyed by IP and minute. Simple and cheap, but allows up to 2x burst across a window boundary. Upgrade path: sliding-window log or token bucket in a Lua script. IP is taken from `X-Forwarded-For` set by nginx; do not trust that header if clients can reach the API nodes directly.

## 11. Monitoring

- `/metrics` exposes per-instance counters: shortens, redirects, cache hits/misses, 404s, expired, rate-limited, DB errors. Scrape with Prometheus.
- Key signals: **cache hit ratio** (hits / (hits+misses)), **p99 redirect latency**, **error rate (5xx)**, **stream lag** (`XLEN clicks` / `XPENDING clicks analytics`), **Postgres connections and replication lag**, **Redis memory and eviction counts**.
- Alerts: hit ratio drops sharply, stream backlog keeps growing, DB error counter rising, `/health` failing.
- Not included yet: structured tracing and dashboards (Grafana).

## 12. Trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Redirect status | 302 | 301 | Analytics and expiry need every click to reach us |
| Code generation | Sequence + base62 | Random / hash | No collisions, no retries; accepts a sequence bottleneck and obfuscation-only secrecy |
| Queue | Redis Streams | Kafka / RabbitMQ | Much simpler at this volume; Kafka wins on very high throughput, multi-consumer replay, long retention |
| Analytics write | Async via queue | Inline insert | Keeps redirect latency independent of analytics DB speed |
| Rate limiter | Fixed window | Sliding window / token bucket | Simplicity; documented boundary burst |
| Unique users | `sha256(ip|ua)` | Cookies / HyperLogLog | No cookies needed; imprecise behind NAT. HyperLogLog would cut storage at the cost of exactness |
| Country | Trust `CF-IPCountry`/`X-Country` header, else `XX` | GeoIP database | No external dependency; wire in MaxMind for real data |
| Postgres | Single primary | Sharded / NoSQL | Sufficient for estimated load; sharding plan documented |

## Benchmark results

_Fill in from `scripts/benchmark.py` after running it on your machine (cold and warm cache). Do not quote the capacity estimates above as measured numbers._

| Run | Req/s | p50 | p95 | p99 | Cache hit ratio |
|---|---|---|---|---|---|
| Cold cache | | | | | |
| Warm cache | | | | | |
