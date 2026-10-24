"""ReMail 自动供号：下单请求体、订单→号池转换、补货优先级。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from webui import db, mail_supply


@pytest.fixture(autouse=True)
def reset_refill_clock(monkeypatch):
    monkeypatch.setattr(mail_supply, "_last_refill_at", 0.0)


def _future_stamp(minutes=45):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _order(email="fixture.mailbox@icloud.com", token="st_fixture", minutes=45):
    return {
        "orderNo": "ORFIXTURE",
        "status": "active",
        "productType": "icloud",
        "serviceMode": "purchase",
        "deliveryEmail": email,
        "serviceToken": token,
        "receiveUntil": _future_stamp(minutes),
        "payAmount": "60.00",
    }


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _fake_session(calls, response):
    class Session:
        def request(self, method, url, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return response

        def close(self):
            pass

    return Session()


def test_config_defaults_follow_verified_api_shape():
    cfg = mail_supply.config()
    assert cfg["project_id"] == 2
    assert cfg["email_suffix"] == "icloud.com"
    assert cfg["auto_buy"] is False
    assert mail_supply.MIN_ORDER_QUANTITY <= cfg["batch_size"] <= mail_supply.MAX_ORDER_QUANTITY
    assert cfg["service_mode"] == "purchase"


def test_batch_buy_sends_project_suffix_quantity_and_idempotency(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mail_supply, "_session",
        lambda: _fake_session(calls, _Response(201, [{
            "index": 0, "status": "succeeded", "order": _order(),
        }])),
    )

    orders = mail_supply.buy(quantity=5)

    assert len(orders) == 1
    # 批量接口把订单包在 item["order"] 里：必须拆出来，否则取不到
    # deliveryEmail/serviceToken（历史事故：买 20 个入库 0 个白扣 1200）
    assert orders[0]["deliveryEmail"] == "fixture.mailbox@icloud.com"
    assert orders[0]["serviceToken"] == "st_fixture"
    call = calls[0]
    assert call["url"].endswith("/v1/open/orders/batch")
    assert call["json"] == {
        "projectId": 2, "emailSuffix": "icloud.com", "quantity": 5,
    }
    assert call["params"]["serviceMode"] == "purchase"
    assert call["headers"]["Idempotency-Key"]


def test_batch_buy_skips_failed_items_and_fetches_missing_token(monkeypatch):
    calls = []
    batch = [
        {"index": 0, "status": "failed", "error": {"code": "insufficient_inventory"}},
        {"index": 1, "status": "succeeded",
         "order": {"orderNo": "ORNEEDS_DETAIL", "deliveryEmail": "detail@icloud.com"}},
    ]
    monkeypatch.setattr(
        mail_supply, "_session",
        lambda: _fake_session(calls, _Response(201, batch)),
    )
    monkeypatch.setattr(
        mail_supply, "order_detail",
        lambda order_no: {"serviceToken": "st_from_detail",
                          "deliveryEmail": "detail@icloud.com"},
    )

    orders = mail_supply.buy(quantity=2)

    assert len(orders) == 1
    assert orders[0]["serviceToken"] == "st_from_detail"


def test_single_buy_uses_single_order_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mail_supply, "_session",
        lambda: _fake_session(calls, _Response(201, _order())),
    )

    orders = mail_supply.buy(quantity=1)

    assert len(orders) == 1
    call = calls[0]
    assert call["url"].endswith("/v1/open/orders")
    assert "quantity" not in call["json"]
    assert call["json"]["emailSuffix"] == "icloud.com"


def test_order_row_builds_relay_pickup_url():
    row = mail_supply.order_to_row(
        {"deliveryEmail": "a.b@icloud.com", "serviceToken": "st_123"}
    )
    assert row["kind"] == "icloud_relay"
    assert row["relay_url"] == (
        "https://remail.aishop6.com/pickup?email=a.b@icloud.com&token=st_123"
    )
    assert mail_supply.order_to_row({"deliveryEmail": "a@icloud.com"}) is None


def test_import_active_orders_skips_expired(monkeypatch):
    orders = [
        _order(email="alive@icloud.com", token="st_alive", minutes=30),
        _order(email="dead@icloud.com", token="st_dead", minutes=-30),
        {"deliveryEmail": "notoken@icloud.com", "receiveUntil": _future_stamp()},
    ]
    monkeypatch.setattr(mail_supply, "active_orders", lambda limit=100: orders)
    monkeypatch.setattr(
        mail_supply, "order_detail",
        lambda order_no: {"deliveryEmail": "notoken@icloud.com",
                          "receiveUntil": _future_stamp()},
    )

    result = mail_supply.import_active_orders()

    assert result["ok"] is True
    assert result["usable"] == 1
    assert result["expired"] == 1
    assert db.get_account("alive@icloud.com")["status"] == "available"
    assert db.get_account("dead@icloud.com") is None
    assert db.get_account("notoken@icloud.com") is None


def test_import_active_orders_falls_back_to_order_detail(monkeypatch):
    """列表接口没有 serviceToken 时，要回查详情再入库。"""
    listed = [{
        "orderNo": "ORLISTONLY",
        "deliveryEmail": "detail.needed@icloud.com",
        "receiveUntil": _future_stamp(30),
    }]
    monkeypatch.setattr(mail_supply, "active_orders", lambda limit=100: listed)
    monkeypatch.setattr(
        mail_supply, "order_detail",
        lambda order_no: _order(email="detail.needed@icloud.com", token="st_detail"),
    )

    result = mail_supply.import_active_orders()

    assert result["usable"] == 1
    account = db.get_account("detail.needed@icloud.com")
    assert account["status"] == "available"
    assert "token=st_detail" in account["relay_url"]


def test_refill_prefers_existing_orders_before_spending(monkeypatch):
    bought = []
    db.set_setting("remail_auto_buy", "1")
    db.set_setting("remail_min_available", "0")
    monkeypatch.setattr(
        mail_supply, "import_active_orders",
        lambda limit=100: (
            mail_supply.import_rows([mail_supply.order_to_row(_order())])
            or {"inserted": 1, "updated": 0, "skipped": 0, "parsed": 1}
        ),
    )
    monkeypatch.setattr(mail_supply, "buy", lambda quantity=None: bought.append(quantity) or [])

    result = mail_supply.refill(reason="test", force=True)

    assert result["source"] == "existing_orders"
    assert result["bought"] == 0
    assert bought == []
    assert db.stats()["available"] >= 1


def test_refill_buys_when_no_usable_orders(monkeypatch):
    db.set_setting("remail_auto_buy", "1")
    monkeypatch.setattr(
        mail_supply, "import_active_orders",
        lambda limit=100: {"ok": True, "orders": 0, "usable": 0,
                           "inserted": 0, "updated": 0, "skipped": 0},
    )
    monkeypatch.setattr(
        mail_supply, "buy",
        lambda quantity=None: [_order(email="bought@icloud.com", token="st_bought")],
    )

    result = mail_supply.refill(quantity=3, reason="test", force=True)

    assert result["source"] == "purchased"
    assert result["bought"] == 1
    assert db.get_account("bought@icloud.com")["status"] == "available"


def test_refill_respects_threshold_without_force(monkeypatch):
    db.set_setting("remail_auto_buy", "1")
    db.import_accounts("idle@icloud.com----https://x/pickup?token=st", kind="icloud_relay")
    monkeypatch.setattr(
        mail_supply, "buy", lambda quantity=None: pytest.fail("不该下单")
    )
    db.set_setting("remail_min_available", "0")

    result = mail_supply.refill(reason="test")

    assert result.get("skipped") is True


@pytest.mark.parametrize("force", [False, True])
def test_disabled_refill_does_not_query_or_buy_even_with_force(monkeypatch, force):
    db.set_setting("remail_auto_buy", "0")
    request = Mock(side_effect=AssertionError("disabled supply must not contact ReMail"))
    monkeypatch.setattr(mail_supply, "_request", request)

    result = mail_supply.refill(force=force)

    assert result["skipped"] is True
    assert result["reason"] == "自动下单已关闭"
    request.assert_not_called()


def test_disabled_status_and_config_api_do_not_contact_remail(monkeypatch):
    from fastapi.testclient import TestClient
    from webui.app import app

    request = Mock(side_effect=AssertionError("disabled status must stay local"))
    monkeypatch.setattr(mail_supply, "_request", request)
    db.set_setting("remail_auto_buy", "1")

    with TestClient(app) as client:
        saved = client.post('/api/mail/supply/config', json={"auto_buy": False})
        assert saved.status_code == 200
        assert saved.json()["config"]["auto_buy"] is False
        status = client.get('/api/mail/supply')
        assert status.status_code == 200
        assert status.json()["config"]["auto_buy"] is False
        assert "balance" not in status.json()
    request.assert_not_called()


def test_refill_rechecks_switch_after_waiting_for_lock(monkeypatch):
    db.set_setting("remail_auto_buy", "1")

    class DisableWhileWaiting:
        def __enter__(self):
            db.set_setting("remail_auto_buy", "0")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(mail_supply, "_refill_lock", DisableWhileWaiting())
    request = Mock(side_effect=AssertionError("queued refill must see disabled switch"))
    monkeypatch.setattr(mail_supply, "_request", request)
    assert mail_supply.refill()["skipped"] is True
    request.assert_not_called()


def test_disabling_during_order_lookup_prevents_purchase(monkeypatch):
    db.set_setting("remail_auto_buy", "1")

    def import_orders():
        db.set_setting("remail_auto_buy", "0")
        return {"inserted": 0, "updated": 0}

    monkeypatch.setattr(mail_supply, "import_active_orders", import_orders)
    buy = Mock(side_effect=AssertionError("switch was turned off"))
    monkeypatch.setattr(mail_supply, "buy", buy)
    assert mail_supply.refill()["skipped"] is True
    buy.assert_not_called()


def test_failed_lookup_cools_down_all_queued_workers(monkeypatch):
    db.set_setting("remail_auto_buy", "1")
    clock = [1000.0]
    monkeypatch.setattr(mail_supply.time, "time", lambda: clock[0])

    def failed_request():
        # Cooldown must start when the request finishes, even after a slow error.
        clock[0] += 45
        raise mail_supply.MailSupplyError("fixture HTTP 401")

    lookup = Mock(side_effect=failed_request)
    monkeypatch.setattr(mail_supply, "import_active_orders", lookup)

    def worker(_):
        try:
            return mail_supply.refill()
        except mail_supply.MailSupplyError:
            return {"failed": True}

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(worker, range(50)))
    assert lookup.call_count == 1
    assert sum(bool(result.get("failed")) for result in results) == 1
    assert sum(result.get("reason") == "冷却中" for result in results) == 49
    clock[0] += 30
    with pytest.raises(mail_supply.MailSupplyError):
        mail_supply.refill()
    assert lookup.call_count == 2
