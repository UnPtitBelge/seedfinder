import hashlib
import hmac
import json
import time

import pytest

from trainscanner import ledger
from trainscanner.config import settings
from trainscanner.routes.stripe_webhook import PAID_JOURNEYS

SECRET = "webhook-signing-value"
URL = "/api/stripe/webhook"


def _event(type_: str, payment_status: str = "paid", order_id: str | None = None) -> bytes:
    metadata = {"journey_ids": "j1"} | ({"order_id": order_id} if order_id else {})
    return json.dumps(
        {
            "id": "evt_1",
            "object": "event",
            "type": type_,
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "object": "checkout.session",
                    "payment_status": payment_status,
                    "metadata": metadata,
                }
            },
        }
    ).encode()


def _sign(payload: bytes, secret: str = SECRET) -> str:
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRET)
    PAID_JOURNEYS.clear()
    yield
    PAID_JOURNEYS.clear()


def test_webhook_requires_secret(client):
    r = client.post(URL, content=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 503


def test_webhook_rejects_bad_signature(client, webhook_secret):
    payload = _event("checkout.session.completed")
    r = client.post(
        URL, content=payload, headers={"stripe-signature": _sign(payload, "another-signing-value")}
    )
    assert r.status_code == 400
    assert PAID_JOURNEYS == {}


def test_webhook_rejects_missing_signature(client, webhook_secret):
    r = client.post(URL, content=_event("checkout.session.completed"))
    assert r.status_code == 400


def test_webhook_marks_journey_paid(client, webhook_secret):
    payload = _event("checkout.session.completed")
    r = client.post(URL, content=payload, headers={"stripe-signature": _sign(payload)})
    assert r.status_code == 200
    assert r.json() == {"received": True}
    assert PAID_JOURNEYS == {"j1": "cs_test_1"}


def test_webhook_ignores_unpaid_session(client, webhook_secret):
    payload = _event("checkout.session.completed", payment_status="unpaid")
    r = client.post(URL, content=payload, headers={"stripe-signature": _sign(payload)})
    assert r.status_code == 200
    assert PAID_JOURNEYS == {}


def _order(points_used: int = 50, points_earned: int = 200) -> tuple[str, str]:
    acc = ledger.sign_in("ada@example.com")
    return acc.id, ledger.create_order(acc.id, ["j1"], points_used, points_earned, 9000, 230_000)


def test_webhook_credits_eco_points_once(client, webhook_secret):
    account_id, order_id = _order()
    payload = _event("checkout.session.completed", order_id=order_id)
    for _ in range(2):  # Stripe retries: must stay idempotent
        assert (
            client.post(URL, content=payload, headers={"stripe-signature": _sign(payload)}).status_code == 200
        )
    acc = ledger.get_account(account_id)
    assert acc.points == 100 - 50 + 200 and acc.trips == 1


def test_webhook_expired_releases_held_points(client, webhook_secret):
    account_id, order_id = _order()
    payload = _event("checkout.session.expired", payment_status="unpaid", order_id=order_id)
    assert client.post(URL, content=payload, headers={"stripe-signature": _sign(payload)}).status_code == 200
    assert ledger.get_account(account_id).points == 100
    assert ledger.get_order(order_id).status == "cancelled"
