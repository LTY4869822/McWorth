"""官方 query-meals 响应 → MenuSnapshot。

只做字段映射与验证，不猜测缺失的商品或价格（方案 5 节职责表）。
"""

from __future__ import annotations

from ..domain.money import yuan_str_to_fen
from ..domain.types import (
    EvidenceRef,
    EvidenceSource,
    MealInfo,
    MenuSnapshot,
    WithOrderRef,
)


def parse_menu(raw: dict, evidence: EvidenceRef, *, store_code: str,
               order_type: int, be_type: int,
               completeness: str = "full") -> MenuSnapshot:
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise ValueError(f"query-meals 响应异常: {raw.get('message', '非对象')}")
    data = raw.get("data") or {}
    meals_raw = data.get("meals") or {}
    if not meals_raw:
        raise ValueError("query-meals 未返回任何餐品，无法构建快照")

    meals: dict[str, MealInfo] = {}
    for code, info in meals_raw.items():
        if not isinstance(info, dict) or "name" not in info:
            continue
        with_order = None
        wo = info.get("withOrder")
        if isinstance(wo, dict) and wo.get("membershipCode") and wo.get("specId"):
            with_order = WithOrderRef(
                membership_code=str(wo["membershipCode"]), spec_id=str(wo["specId"])
            )
        meals[str(code)] = MealInfo(
            code=str(code),
            name=str(info.get("name", "")),
            current_price_fen=yuan_str_to_fen(str(info["currentPrice"])),
            original_price_fen=yuan_str_to_fen(str(info["originalPrice"])),
            discount_type=info.get("discountType"),
            with_order=with_order,
        )

    categories: list[tuple[str, list[str]]] = []
    for cat in data.get("categories") or []:
        name = (cat.get("name") or "").strip()
        codes = [str(m["code"]) for m in cat.get("meals") or [] if "code" in m]
        if name and codes:
            categories.append((name.replace("\n", ""), codes))

    return MenuSnapshot(
        store_code=store_code,
        order_type=order_type,
        be_type=be_type,
        meals=meals,
        categories=categories,
        evidence=evidence,
        completeness=completeness,  # type: ignore[arg-type]
    )


def snapshot_evidence(trace_id: str, captured_at: str) -> EvidenceRef:
    """为实测快照构建证据引用。"""
    return EvidenceRef(
        source=EvidenceSource.SNAPSHOT,
        tool="query-meals",
        captured_at=captured_at,
        trace_id=trace_id,
    )
