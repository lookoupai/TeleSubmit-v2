"""
二维码生成工具（用于在 Telegram 中展示收款二维码）

依赖：
- segno
- pypng（用于输出 PNG）
"""

from __future__ import annotations

import io
from typing import Optional


def make_qr_png_bytes(text: str, *, scale: int = 8, border: int = 2) -> bytes:
    """
    生成二维码 PNG（二进制）。

    设计取舍（KISS）：
    - 不强行拼接链上 URI（不同链规则不同），默认仅编码 text（通常为收款地址）。
    - 缺少依赖时抛出异常，由调用方决定降级策略（例如仅发文字 + 支付页按钮）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("QR text 不能为空")

    segno = _try_import_segno()
    if segno is None:
        raise RuntimeError("缺少依赖：segno（以及输出 PNG 需要 pypng）")

    qr = segno.make(text)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=border)
    return buf.getvalue()


def _try_import_segno() -> Optional[object]:
    try:
        import segno  # type: ignore
    except Exception:
        return None
    return segno

