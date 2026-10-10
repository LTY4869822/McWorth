"""官方 query-meal-detail 响应 → ComboConfig。

套餐（多轮选配结构）是购买组合重组的核心数据源。
diffPrice 形如 "+ ¥1.5" / "- ¥0.5" / "+ ¥0"，经 Decimal 定点转为带符号分。
"""

from __future__ import annotations

from ..domain.money import yuan_str_to_fen
from ..domain.types import (
    ComboChoice,
    ComboConfig,
    ComboRound,
    EvidenceRef,
)


def _choice_max_quantity(c: dict) -> int | None:
    """选项级数量上限（v0.2.1 F06）。

    官方字段 maxQuantity：正整数 = 真实上限；-1 等负值是"未设置"哨兵，
    语义以真实接口约定为准——此处一律视为无上限（None），不猜测。
    quantity（默认选中量）不是容量，不得混用。
    """
    raw = c.get("maxQuantity")
    if raw is None:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_combo_detail(raw: dict, evidence: EvidenceRef) -> ComboConfig:
    if not isinstance(raw, dict) or raw.get("success") is not True:
        raise ValueError(f"query-meal-detail 响应异常: {raw.get('message', '非对象')}")
    data = raw.get("data") or {}
    rounds: list[ComboRound] = []
    for rnd in data.get("rounds") or []:
        choices = [
            ComboChoice(
                code=str(c["code"]),
                name=str(c.get("name", "")),
                diff_price_fen=yuan_str_to_fen(str(c.get("diffPrice", "+ ¥0"))),
                is_default=int(c.get("isDefault", 0)) == 1,
                support_modify=bool(c.get("supportModify", False)),
                max_quantity=_choice_max_quantity(c),
            )
            for c in rnd.get("choices") or []
            if "code" in c
        ]
        rounds.append(
            ComboRound(
                round_id=int(rnd["id"]),
                name=str(rnd.get("name", "")),
                min_quantity=int(rnd.get("minQuantity", 1)),
                max_quantity=int(rnd.get("maxQuantity", 1)),
                choices=choices,
            )
        )
    if not rounds:
        raise ValueError(f"{data.get('code')} 未返回轮次结构，可能不是套餐")
    return ComboConfig(
        code=str(data["code"]),
        name=str(data.get("name", "")),
        rounds=rounds,
        evidence=evidence,
    )


def cheapest_fill(round_: ComboRound) -> ComboChoice:
    """某一轮在不违背默认结构下的最低差价填充（用于估算参考总价）。"""
    return min(round_.choices, key=lambda c: c.diff_price_fen)


def default_fill(round_: ComboRound) -> ComboChoice:
    for choice in round_.choices:
        if choice.is_default:
            return choice
    return round_.choices[0]
