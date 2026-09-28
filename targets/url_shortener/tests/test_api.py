from datetime import datetime, timedelta, timezone

from app import main, models, services

URL = "https://example.com/some/long/path"


def create(client, **extra):
    return client.post("/api/v1/urls", json={"long_url": URL, **extra})


def test_create_redirect_and_analytics(client):
    r = create(client)
    assert r.status_code == 201
    code = r.json()["short_code"]
    assert len(code) == services.CODE_LENGTH and r.json()["short_url"].endswith(f"/{code}")

    hit = client.get(f"/{code}", follow_redirects=False, headers={"referer": "https://news.example", "user-agent": "pytest"})
    assert hit.status_code == 302 and hit.headers["location"] == URL and hit.headers["cache-control"] == "no-store"
    client.get(f"/{code}", follow_redirects=False, headers={"referer": "https://news.example"})

    a = client.get(f"/api/v1/urls/{code}/analytics").json()
    assert a["total_clicks"] == 2 and a["last_accessed"] is not None
    assert a["top_referrers"] == [{"referrer": "https://news.example", "clicks": 2}]
    assert sum(d["clicks"] for d in a["clicks_by_day"]) == 2 and len(a["recent_clicks"]) == 2


def test_unknown_code_404_for_redirect_and_analytics(client):
    assert client.get("/doesnotexist", follow_redirects=False).status_code == 404
    assert client.get("/api/v1/urls/doesnotexist/analytics").status_code == 404


def test_invalid_inputs_rejected(client):
    assert client.post("/api/v1/urls", json={"long_url": "not-a-url"}).status_code == 422
    assert client.post("/api/v1/urls", json={"long_url": "javascript:alert(1)"}).status_code == 422
    assert create(client, custom_alias="a b!").status_code == 422
    assert create(client, custom_alias="ab").status_code == 422


def test_custom_alias_conflict_409(client):
    assert create(client, custom_alias="promo").status_code == 201
    assert create(client, custom_alias="promo").status_code == 409


def test_reserved_alias_rejected(client):
    assert create(client, custom_alias="docs").status_code == 422
    assert create(client, custom_alias="Health").status_code == 422


def test_expiry_in_past_rejected_and_tz_aware_accepted(client):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert create(client, expires_at=past).status_code == 422
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()  # tz-aware must not 500
    assert create(client, expires_at=future).status_code == 201


def test_expired_link_returns_410(client, session_factory):
    code = create(client).json()["short_code"]
    with session_factory() as db:
        db.query(models.URL).update({"expires_at": models.utcnow() - timedelta(seconds=5)})
        db.commit()
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


def test_code_collision_is_retried(client, monkeypatch):
    create(client, custom_alias="taken12")
    codes = iter(["taken12", "taken12", "fresh99"])
    monkeypatch.setattr(services, "generate_code", lambda length=7: next(codes))
    assert create(client).json()["short_code"] == "fresh99"


def test_collision_exhaustion_returns_503(client, monkeypatch):
    create(client, custom_alias="taken12")
    monkeypatch.setattr(services, "generate_code", lambda length=7: "taken12")
    assert create(client).status_code == 503


def test_redirect_survives_click_write_failure(client, monkeypatch):
    code = create(client).json()["short_code"]
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import OperationalError

    real_commit = Session.commit
    state = {"n": 0}

    def flaky_commit(self):
        state["n"] += 1
        raise OperationalError("insert", {}, Exception("db down"))

    monkeypatch.setattr(Session, "commit", flaky_commit)
    resp = client.get(f"/{code}", follow_redirects=False)
    monkeypatch.setattr(Session, "commit", real_commit)
    assert resp.status_code == 302  # analytics failure did not break the redirect
    assert "click_write_failures_total 1" in client.get("/metrics").text


def test_rate_limit_returns_429(client):
    main.limiter.limit = 2
    assert [create(client).status_code for _ in range(3)] == [201, 201, 429]


def test_health_ready_metrics_and_request_id(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/", follow_redirects=False).headers["location"] == "/docs"  # landing page, not a 404
    assert client.get("/favicon.ico").status_code == 204  # browser icon request is not an unknown short code
    g = client.get("/ops/golden-signals?window=60").json()  # golden signals: latency, traffic, errors, saturation
    assert {"latency", "traffic", "errors", "saturation", "slos"} <= set(g) and g["traffic"]["requests"] >= 1
    assert client.get("/ops/dashboard").headers["content-type"].startswith("text/html")
    assert client.get("/ready").json() == {"status": "ready"}
    r = create(client)
    assert r.headers["x-request-id"]
    text = client.get("/metrics").text
    assert 'http_requests_total{method="POST",route="/api/v1/urls",status="201"} 1' in text
    assert "urls_created_total 1" in text
