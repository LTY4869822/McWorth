"""内部购物车结构 → 官方 calculate-price 请求参数（优化方案 B1）。

宿主（Agent）按本模块的输出直接调用官方 MCP `calculate-price`，
字段转换由适配器统一完成，避免每次靠模型临时改写。
轮次 id 官方参数名为字符串 `round`；子项无定制（本版本不支持
套餐内定制，带定制需求已在上游明确拒绝）。
"""

from __future__ import annotations

from typing import Any

from ..domain.types import OfferItem


def to_official_payload(
    offer: list[OfferItem], store_code: str, order_type: int, be_type: int
) -> dict[str, Any]:
    """把一条候选购买路径转换为官方 calculate-price 的请求参数。"""
    items: list[dict[str, Any]] = []
    for o in sorted(offer, key=lambda x: x.product_code):
        entry: dict[str, Any] = {
            "productCode": o.product_code,
            "quantity": o.quantity,
        }
        if o.rounds:
            entry["roundList"] = [
                {
                    "round": r.round_id,
                    "comboItemList": [
                        {"code": i.code, "quantity": i.quantity}
                        for i in sorted(r.items, key=lambda i: (i.code, i.quantity))
                    ],
                }
                for r in sorted(o.rounds, key=lambda r: r.round_id)
            ]
        items.append(entry)
    return {
        "storeCode": store_code,
        "orderType": order_type,
        "beType": be_type,
        "items": items,
    }
