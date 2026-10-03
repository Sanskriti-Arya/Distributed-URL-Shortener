"""End-to-end tests against the running stack: `docker compose up -d --build` first."""
import os
import time
import uuid

import httpx
import pytest

BASE = os.getenv("BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def client():
    c = httpx.Client(base_url=BASE, follow_redirects=False, timeout=5)
    try:
        c.get("/health")
    except httpx.TransportError:
        pytest.skip("stack not running (docker compose up -d --build)")
    return c


def test_shorten_and_redirect(client):
    r = client.post("/shorten", json={"url": "https://example.com/some/long/path"})
    assert r.status_code == 201
    code = r.json()["code"]
    assert r.json()["short_url"].endswith("/" + code)
    for _ in range(2):  # first = cache miss, second = cache hit
        g = client.get(f"/{code}")
        assert g.status_code == 302 and g.headers["location"] == "https://example.com/some/long/path"


def test_custom_alias_and_conflict(client):
    alias = "t" + uuid.uuid4().hex[:8]
    assert client.post("/shorten", json={"url": "https://example.com", "custom_alias": alias}).status_code == 201
    assert client.post("/shorten", json={"url": "https://example.org", "custom_alias": alias}).status_code == 409


def test_validation_errors(client):
    assert client.post("/shorten", json={"url": "not a url"}).status_code == 422
    assert client.post("/shorten", json={"url": "https://e.com", "custom_alias": "shorten"}).status_code == 422


def test_unknown_code_404(client):
    assert client.get("/doesNotExist1").status_code == 404


def test_expiration(client):
    code = client.post("/shorten", json={"url": "https://example.com", "expires_in_seconds": 1}).json()["code"]
    assert client.get(f"/{code}").status_code == 302
    time.sleep(2.2)
    assert client.get(f"/{code}").status_code == 410


def test_analytics_eventually_consistent(client):
    code = client.post("/shorten", json={"url": "https://example.com/a"}).json()["code"]
    for _ in range(3):
        client.get(f"/{code}", headers={"Referer": "https://google.com/search", "X-Country": "IN"})
    deadline = time.time() + 15
    data = {}
    while time.time() < deadline:
        data = client.get(f"/analytics/{code}").json()
        if data.get("clicks") == 3:
            break
        time.sleep(0.5)
    assert data["clicks"] == 3
    assert data["top_referrer"] == "google.com" and data["top_country"] == "IN"


def test_load_is_spread_across_servers(client):
    code = client.post("/shorten", json={"url": "https://example.com/lb"}).json()["code"]
    served = {client.get(f"/{code}").headers["x-served-by"] for _ in range(30)}
    assert len(served) >= 2
