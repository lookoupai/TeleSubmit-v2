import pytest

from utils.upay_pro_client import build_signature, verify_signature


def test_verify_signature_accepts_both_key_concat_styles():
    secret_key = "test_secret"
    payload = {
        "trade_id": "T123",
        "order_id": "AD202501010001",
        "amount": 10,
        "actual_amount": 10.01,
        "token": "USDT-TRC20",
        "block_transaction_id": "0xabc",
        "status": 2,
    }

    sig1 = build_signature(payload, secret_key, append_ampersand_before_key=False)
    assert verify_signature({**payload, "signature": sig1}, secret_key)

    sig2 = build_signature(payload, secret_key, append_ampersand_before_key=True)
    assert verify_signature({**payload, "signature": sig2}, secret_key)

