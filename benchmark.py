"""Redirect benchmark. Start the stack with the rate limit raised:

    RATE_LIMIT_PER_MIN=1000000 docker compose up -d --build
    python scripts/benchmark.py --urls 2000 --requests 20000 --concurrency 100

Traffic is Zipf-skewed (a few hot links, a long tail), like real redirect traffic.
Run it twice (once right after startup = cold cache, once again = warm) and record both in DESIGN.md.
"""
import argparse
import asyncio
import random
import statistics
import time
from collections import Counter

import httpx


async def main(a):
    limits = httpx.Limits(max_connections=a.concurrency, max_keepalive_connections=a.concurrency)
    async with httpx.AsyncClient(base_url=a.base, limits=limits, follow_redirects=False, timeout=10) as c:
        sem = asyncio.Semaphore(a.concurrency)

        async def make(i):
            async with sem:
                r = await c.post("/shorten", json={"url": f"https://example.com/page/{i}"})
                r.raise_for_status()
                return r.json()["code"]

        t0 = time.perf_counter()
        codes = await asyncio.gather(*(make(i) for i in range(a.urls)))
        print(f"created {a.urls} urls in {time.perf_counter()-t0:.1f}s")

        weights = [1 / (rank + 1) for rank in range(len(codes))]
        picks = random.choices(codes, weights=weights, k=a.requests)
        latencies, status, servers = [], Counter(), Counter()

        async def hit(code):
            async with sem:
                s = time.perf_counter()
                r = await c.get(f"/{code}")
                latencies.append((time.perf_counter() - s) * 1000)
                status[r.status_code] += 1
                servers[r.headers.get("x-served-by")] += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(hit(p) for p in picks))
        wall = time.perf_counter() - t0

    latencies.sort()
    pct = lambda p: latencies[min(len(latencies) - 1, int(len(latencies) * p))]
    print(f"{a.requests} redirects, concurrency {a.concurrency}: {a.requests/wall:,.0f} req/s")
    print(f"latency ms  p50={pct(.5):.1f}  p95={pct(.95):.1f}  p99={pct(.99):.1f}  mean={statistics.mean(latencies):.1f}")
    print("status:", dict(status), " servers:", dict(servers))
    print("cache hit ratio: see  curl localhost:8000/metrics  (per instance)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--urls", type=int, default=2000)
    p.add_argument("--requests", type=int, default=20000)
    p.add_argument("--concurrency", type=int, default=100)
    asyncio.run(main(p.parse_args()))
