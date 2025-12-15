from decimal import Decimal

from utils.paid_ad_service import parse_upay_create_order_response


def test_parse_upay_create_order_response_extracts_fields_and_converts_millis_to_seconds():
    resp = {
        "status_code": 200,
        "message": "success",
        "data": {
            "trade_id": "T123",
            "order_id": "AD202512150001",
            "amount": 10.0,
            "actual_amount": 10.01,
            "token": "TXYZ...",
            "expiration_time": 1730000000000,  # ms
            "payment_url": "https://pay.example.com/pay/checkout-counter/T123",
        },
    }

    parsed = parse_upay_create_order_response(resp)

    assert parsed["trade_id"] == "T123"
    assert parsed["payment_url"] == "https://pay.example.com/pay/checkout-counter/T123"
    assert parsed["pay_address"] == "TXYZ..."
    assert parsed["pay_amount"] == Decimal("10.01")
    assert parsed["expires_at"] == 1730000000.0

