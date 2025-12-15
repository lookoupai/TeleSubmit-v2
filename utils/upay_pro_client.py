"""
UPAY_PRO 客户端（最小封装）
"""
import hashlib
import logging
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def build_signature(params: Dict[str, Any], secret_key: str, *, append_ampersand_before_key: bool = False) -> str:
    """
    按 UPAY_PRO 规则生成签名：
    - 仅参与非空字段
    - key 按字母序排序
    - 使用 & 拼接为 key=value
    - 末尾拼接 secret_key（部分实现会额外拼接一个 &，提供兼容开关）
    """
    def _value_to_string(v: Any) -> str:
        if isinstance(v, Decimal):
            return format(v.normalize(), "f")
        if isinstance(v, (int, float)):
            return format(Decimal(str(v)).normalize(), "f")
        return str(v)

    pairs = []
    for k, v in params.items():
        if v is None:
            continue
        v_str = _value_to_string(v).strip()
        if v_str == "":
            continue
        pairs.append((str(k), v_str))
    pairs.sort(key=lambda x: x[0])
    param_str = "&".join([f"{k}={v}" for k, v in pairs])
    if append_ampersand_before_key and param_str:
        param_str = f"{param_str}&{secret_key}"
    else:
        param_str = f"{param_str}{secret_key}"
    return _md5_hex(param_str)


def verify_signature(payload: Dict[str, Any], secret_key: str) -> bool:
    signature = str(payload.get("signature", "") or "").strip().lower()
    if not signature:
        return False
    params = {k: v for k, v in payload.items() if k != "signature"}
    sig1 = build_signature(params, secret_key, append_ampersand_before_key=False).lower()
    if signature == sig1:
        return True
    sig2 = build_signature(params, secret_key, append_ampersand_before_key=True).lower()
    return signature == sig2


def normalize_amount(amount: Any, *, decimals: int = 2) -> Decimal:
    d = Decimal(str(amount))
    q = Decimal("1." + ("0" * decimals))
    return d.quantize(q)


async def create_order(
    *,
    base_url: str,
    secret_key: str,
    order_id: str,
    amount: Decimal,
    type_: str,
    notify_url: str,
    redirect_url: str,
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/create_order"

    body: Dict[str, Any] = {
        "type": type_,
        "order_id": order_id,
        "amount": float(amount),
        "notify_url": notify_url,
        "redirect_url": redirect_url,
    }
    signature_params = dict(body)
    signature_params["amount"] = format(amount.normalize(), "f")
    body["signature"] = build_signature(signature_params, secret_key)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"UPAY_PRO create_order HTTP {resp.status}: {text[:500]}")
            try:
                data = await resp.json()
            except Exception as e:
                raise RuntimeError(f"UPAY_PRO create_order 响应非 JSON: {text[:500]}") from e

    return data


async def check_status(
    *,
    base_url: str,
    trade_id: str,
    timeout_seconds: int = 10,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/pay/check-status/{trade_id}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"UPAY_PRO check-status HTTP {resp.status}: {text[:500]}")
            try:
                data = await resp.json()
            except Exception as e:
                raise RuntimeError(f"UPAY_PRO check-status 响应非 JSON: {text[:500]}") from e
    return data
