"""Generated for the max_clicks enhancement."""
URL = "https://example.com/limited"


def create(client, **extra):
    return client.post("/api/v1/urls", json={"long_url": URL, **extra})


def test_limit_enforced_then_410(client):
    r = create(client, max_clicks=2)
    assert r.status_code == 201 and r.json()["max_clicks"] == 2
    code = r.json()["short_code"]
    statuses = [client.get(f"/{code}", follow_redirects=False).status_code for _ in range(3)]
    assert statuses == [302, 302, 410]


def test_analytics_reports_remaining_clicks(client):
    code = create(client, max_clicks=3).json()["short_code"]
    client.get(f"/{code}", follow_redirects=False)
    a = client.get(f"/api/v1/urls/{code}/analytics").json()
    assert a["total_clicks"] == 1 and a["max_clicks"] == 3 and a["remaining_clicks"] == 2


def test_unlimited_by_default_is_backward_compatible(client):
    r = create(client)
    assert r.status_code == 201 and r.json()["max_clicks"] is None
    code = r.json()["short_code"]
    assert all(client.get(f"/{code}", follow_redirects=False).status_code == 302 for _ in range(5))
    assert client.get(f"/api/v1/urls/{code}/analytics").json()["remaining_clicks"] is None


def test_max_clicks_must_be_positive(client):
    assert create(client, max_clicks=0).status_code == 422
