"""官方 calculate-price 响应 → VerifiedQuote（优化方案 A5 修订）。

金额规则：
- 实付/商品/原价必须是非负整数分；布尔、小数、负数、非有限值一律拒绝；
- 缺失的费用（配送/打包/餐具/优惠）保持 None（未知），绝不冒充 0；
- 官方总额与明细口径不一致时保留原始值，由上层决定是否标记诊断，不推造补差。
"""

from __future__ import annotations

from ..domain.money import MoneyError
from ..domain.types import (
    EvidenceRef,
    EvidenceSource,
    QuoteItem,
    VerifiedQuote,
)


def _required_fen(value: object, field: str) -> int:
    """必填金额：非负整数分。拒绝 bool / float / 负数 / 其他类型。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"{field} 必须为整数分，得到 {type(value).__name__}: {value!r}")
    if value < 0:
        raise MoneyError(f"{field} 不能为负数: {value!r}")
    return value


def _optional_fen(value: object, field: str) -> int | None:
    """可选金额：字段缺失（或显式 null）= 未知（None）；存在则同必填规则。"""
    if value is None:
        return None
    return _required_fen(value, field)


def parse_quote(raw: dict, evidence: EvidenceRef) -> VerifiedQuote:
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise ValueError(f"calculate-price 响应异常: {raw.get('message', '非对象')}")
    data = raw.get("data") or {}
    if "price" not in data:
        raise ValueError("calculate-price 缺少实付总额 price，不可作为有效报价")
    items = []
    for it in data.get("productList") or []:
        if "productCode" not in it:
            continue
        items.append(
            QuoteItem(
                product_code=str(it["productCode"]),
                product_name=str(it.get("productName", "")),
                quantity=_required_fen(it.get("quantity", 1), "productList.quantity"),
                original_subtotal_fen=_optional_fen(it.get("originalSubtotal"), "originalSubtotal"),
                subtotal_fen=_optional_fen(it.get("subtotal"), "subtotal"),
            )
        )
    return VerifiedQuote(
        total_fen=_required_fen(data["price"], "data.price"),
        product_price_fen=_optional_fen(data.get("productPrice"), "data.productPrice"),
        original_total_fen=_optional_fen(data.get("originalPrice"), "data.originalPrice"),
        discount_fen=_optional_fen(data.get("discount"), "data.discount"),
        delivery_fen=_optional_fen(data.get("deliveryPrice"), "data.deliveryPrice"),
        packing_fen=_optional_fen(data.get("packingPrice"), "data.packingPrice"),
        tableware_fen=_optional_fen(data.get("tablewarePrice"), "data.tablewarePrice"),
        items=items,
        evidence=evidence,
    )


def snapshot_quote_evidence(trace_id: str, captured_at: str) -> EvidenceRef:
    return EvidenceRef(
        source=EvidenceSource.SNAPSHOT,
        tool="calculate-price",
        captured_at=captured_at,
        trace_id=trace_id,
    )
