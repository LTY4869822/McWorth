"""官方 calculate-price 响应 → VerifiedQuote。

金额单位为“分”（官方返回即整数分）。只取官方确实返回的字段，
明细与总额不一致时保留原始证据并标记，不用模型补数。
"""

from __future__ import annotations

from ..domain.types import (
    EvidenceRef,
    EvidenceSource,
    QuoteItem,
    VerifiedQuote,
)


def parse_quote(raw: dict, evidence: EvidenceRef) -> VerifiedQuote:
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise ValueError(f"calculate-price 响应异常: {raw.get('message', '非对象')}")
    data = raw.get("data") or {}
    if "price" not in data:
        raise ValueError("calculate-price 缺少实付总额 price，不可作为有效报价")
    items = [
        QuoteItem(
            product_code=str(it["productCode"]),
            product_name=str(it.get("productName", "")),
            quantity=int(it.get("quantity", 1)),
            original_subtotal_fen=int(it.get("originalSubtotal", 0)),
            subtotal_fen=int(it.get("subtotal", 0)),
        )
        for it in data.get("productList") or []
        if "productCode" in it
    ]
    status = "official" if evidence.source == EvidenceSource.OFFICIAL else "snapshot"
    return VerifiedQuote(
        total_fen=int(data["price"]),
        product_price_fen=int(data.get("productPrice", data["price"])),
        original_total_fen=int(data.get("originalPrice", data["price"])),
        discount_fen=int(data.get("discount", 0)),
        delivery_fen=int(data.get("deliveryPrice", 0)),
        packing_fen=int(data.get("packingPrice", 0)),
        tableware_fen=int(data.get("tablewarePrice", 0)),
        items=items,
        evidence=evidence,
        quote_status=status,  # type: ignore[arg-type]
    )


def snapshot_quote_evidence(trace_id: str, captured_at: str) -> EvidenceRef:
    return EvidenceRef(
        source=EvidenceSource.SNAPSHOT,
        tool="calculate-price",
        captured_at=captured_at,
        trace_id=trace_id,
    )
