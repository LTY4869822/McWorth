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


def quote_consistency_problems(quote: VerifiedQuote) -> list[str]:
    """A6（v0.2.2）：报价金额自洽校验——判断题从"这条报价属于谁"升级为"这条报价是否自相矛盾"。

    身份四元组只回答"这条报价属于哪个请求"，不回答"这条报价自己站不站得住"。
    官方响应内部的金额字段必须能互相解释：

        productPrice = Σ items.subtotal
        total        = productPrice - discount + delivery + packing + tableware

    缺失组件（None）表示**未知**，绝不当作 0，因此组件不全时只做单侧可证伪的检查：
    费用只会加、优惠只会减，所以「商品价 - 已知优惠」是实付的**下限**；
    只有全部组件齐备时才要求与公式**完全相等**。
    """
    problems: list[str] = []
    product_price = quote.product_price_fen

    # ① 明细合计必须等于商品价（两者都在时才可判）
    if product_price is not None and quote.items:
        subtotals = [i.subtotal_fen for i in quote.items]
        if all(s is not None for s in subtotals):
            item_sum = sum(s for s in subtotals if s is not None)
            if item_sum != product_price:
                problems.append(f"明细合计 {item_sum} 分与商品价 {product_price} 分不一致")

    # ② 实付必须落在「商品价 - 优惠 + 费用」可解释的区间内
    if product_price is not None:
        discount = quote.discount_fen
        fees = (quote.delivery_fen, quote.packing_fen, quote.tableware_fen)
        fees_all_known = all(f is not None for f in fees)
        fee_sum = sum(f for f in fees if f is not None)

        if discount is not None:
            floor = product_price - discount
            if quote.total_fen < floor:
                problems.append(
                    f"实付 {quote.total_fen} 分低于「商品价 {product_price} - 优惠 {discount}」"
                    f"可得的下限 {floor} 分"
                )
            if fees_all_known and quote.total_fen != floor + fee_sum:
                problems.append(
                    f"实付 {quote.total_fen} 分与「商品价 {product_price} - 优惠 {discount} "
                    f"+ 费用 {fee_sum}」= {floor + fee_sum} 分不符"
                )
        elif fees_all_known and quote.total_fen > product_price + fee_sum:
            problems.append(
                f"实付 {quote.total_fen} 分高于「商品价 {product_price} + 费用 {fee_sum}」"
                f"的上限 {product_price + fee_sum} 分"
            )

    return problems
