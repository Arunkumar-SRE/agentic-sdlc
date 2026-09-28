"""Generated from the agreed API contract: fails on contract drift."""
from app.main import app

CONTRACT_OPERATIONS = {
    "/api/v1/urls": [
        "post"
    ],
    "/api/v1/urls/{short_code}/analytics": [
        "get"
    ],
    "/health": [
        "get"
    ],
    "/metrics": [
        "get"
    ],
    "/ops/golden-signals": [
        "get"
    ],
    "/ready": [
        "get"
    ],
    "/{short_code}": [
        "get"
    ]
}
CREATE_FIELDS = ["custom_alias", "expires_at", "long_url"]


def test_every_contract_operation_is_served():
    served = app.openapi()["paths"]
    missing = [f"{m.upper()} {p}" for p, methods in CONTRACT_OPERATIONS.items() for m in methods if m not in served.get(p, {})]
    assert not missing, missing


def test_create_request_accepts_contract_fields():
    props = app.openapi()["components"]["schemas"]["URLCreate"]["properties"]
    assert set(CREATE_FIELDS) <= set(props), set(CREATE_FIELDS) - set(props)
