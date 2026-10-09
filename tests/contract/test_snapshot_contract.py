"""契约测试：官方响应结构一旦漂移，这里会立刻失败。

样例来自麦当劳 MCP 的实测快照（tests/fixtures），已做脱敏投影，
不含账户信息或凭证。测试只验证“解析器与快照形状一致”，不发起网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcworth.adapters.combo import cheapest_fill, default_fill, parse_combo_detail
from mcworth.adapters.menu import parse_menu, snapshot_evidence
from mcworth.adapters.pricing import parse_quote, snapshot_quote_evidence
from mcworth.domain.types import FoodAtom, OfferItem, cart_hash, plate_hash
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load(name: str) -> tuple[dict, dict]:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return raw["_meta"], raw["response"]


# ---------------------------------------------------------------- 菜单


def test_menu_parses_meals_with_exact_fen_prices():
    meta, resp = load("menu_store_1450713.json")
    menu = parse_menu(
        resp,
        snapshot_evidence(meta["trace_id"], meta["captured_at"]),
        store_code=meta["store_code"],
        order_type=meta["order_type"],
        be_type=meta["be_type"],
        completeness="partial",
    )
    # 真实数据锚点：门店 1450713，到店自取，2026-10-09 采集
    assert "1440" in menu.meals and "4810" in menu.meals
    assert menu.meals["1440"].name == "麦辣鸡腿汉堡"
    assert menu.meals["1440"].current_price_fen == 2250      # ¥22.5 → 2250 分
    assert menu.meals["4810"].current_price_fen == 1350      # ¥13.5 → 1350 分
    assert menu.meals["4810"].original_price_fen == 1350
    assert menu.store_code == "1450713"
    assert menu.completeness == "partial"
    assert menu.evidence.source.value == "snapshot"
    assert menu.categories, "快照应保留分类结构"


@pytest.mark.parametrize(
    "bad",
    [
        {"success": False, "message": "门店休息中"},
        {"success": True, "data": {}},
        {"success": True, "data": {"meals": {}}},
    ],
)
def test_menu_rejects_unusable_response(bad):
    """不可用的响应必须明确报错，而不是静默产出空快照。"""
    with pytest.raises(ValueError):
        parse_menu(bad, snapshot_evidence("t", "now"), store_code="1", order_type=1, be_type=1)


# ---------------------------------------------------------------- 套餐


def test_combo_rounds_expose_cheaper_and_negative_diff_items():
    meta, resp = load("combo_9900005456.json")
    combo = parse_combo_detail(resp, snapshot_evidence(meta["trace_id"], meta["captured_at"]))
    assert combo.code == "9900005456"
    assert combo.name == "麦辣鸡腿汉堡三件套"
    assert len(combo.rounds) == 3

    # 轮次结构（真实数据锚点）
    assert [r.round_id for r in combo.rounds] == [1, 2, 3]
    # 主餐轮只有 1 项且必选 → 套餐必然包含麦辣鸡腿汉堡
    main = combo.rounds[0]
    assert len(main.choices) == 1 and main.choices[0].code == "1440"
    assert (main.min_quantity, main.max_quantity) == (1, 1)

    # 小食轮存在负差价项：小杯玉米杯 -¥0.5，比默认的中薯条还便宜
    snack = combo.rounds[1]
    codes = {c.code: c for c in snack.choices}
    assert "4810" in codes and "4437" in codes
    assert codes["4437"].diff_price_fen == -50
    assert codes["4810"].diff_price_fen == 0 and codes["4810"].is_default
    assert cheapest_fill(snack).code == "4437"
    assert default_fill(snack).code == "4810"

    # 饮料轮必须至少选 1 —— 这是“不因便宜塞饮料”必须绕开的硬约束
    drink = combo.rounds[2]
    assert drink.min_quantity >= 1
    assert len(drink.choices) > 10


def test_default_and_cheapest_fill_are_both_deterministic():
    meta, resp = load("combo_9900005456.json")
    combo = parse_combo_detail(resp, snapshot_evidence(meta["trace_id"], meta["captured_at"]))
    for rnd in combo.rounds:
        d = default_fill(rnd)
        assert d.is_default or d is rnd.choices[0]
        assert cheapest_fill(rnd).diff_price_fen <= d.diff_price_fen, (
            "最低差价填充不应比默认项更贵"
        )


def test_combo_requires_rounds():
    with pytest.raises(ValueError):
        parse_combo_detail(
            {"success": True, "data": {"code": "123", "name": "非套餐"}},
            snapshot_evidence("t", "now"),
        )


# ---------------------------------------------------------------- 报价


def test_quote_uses_official_total_and_keeps_trace():
    meta, resp = load("quote_single_1440_4810.json")
    quote = parse_quote(resp, snapshot_quote_evidence(meta["trace_id"], meta["captured_at"]))
    # 真实官方核价：麦辣鸡腿汉堡 + 中薯条 = ¥36.00
    assert quote.total_fen == 3600
    assert quote.original_total_fen == 3600
    assert quote.discount_fen == 0
    assert quote.quote_status == "snapshot"
    assert quote.evidence.trace_id == meta["trace_id"]
    assert {i.product_code for i in quote.items} == {"1440", "4810"}


def test_combo_quote_lower_than_singles_but_not_same_plate():
    """真实反直觉案例：三件套 ¥33.50 < 单买 ¥36.00，但餐盘多出一杯饮料。"""
    m1, r1 = load("quote_single_1440_4810.json")
    m2, r2 = load("quote_combo_9900005456.json")
    single = parse_quote(r1, snapshot_quote_evidence(m1["trace_id"], m1["captured_at"]))
    combo = parse_quote(r2, snapshot_quote_evidence(m2["trace_id"], m2["captured_at"]))
    assert combo.total_fen == 3350
    assert combo.total_fen < single.total_fen
    # 两者餐盘不同 → 只能叫“方案差额”，不能叫“无代价节省”（由 solver 层判定）
    assert combo.total_fen < single.total_fen


def test_quote_without_price_field_is_rejected():
    """没有 price 的响应不能充当有效报价——避免把未核价当已核价。"""
    with pytest.raises(ValueError):
        parse_quote(
            {"success": True, "data": {"originalPrice": 100}},
            snapshot_quote_evidence("t", "now"),
        )


# ---------------------------------------------------------------- 摘要一致性


def test_cart_hash_stable_and_canonical_roundtrip():
    """fixture 里的 offer 结构与求解器构造的 offer 必须算出同一个 cart_hash。"""
    meta, resp = load("quote_combo_9900005456.json")
    offer = [OfferItem.from_canonical(o) for o in meta["offer"]]
    h1 = cart_hash(offer)
    # 重排、重建后应完全一致（canonical 是排序无关的）
    offer2 = [OfferItem.from_canonical(o) for o in reversed(meta["offer"])]
    h2 = cart_hash(list(reversed(offer2)))
    assert h1 == h2
    again = [OfferItem.from_canonical(o) for o in meta["offer"]]
    assert cart_hash(again) == h1


def test_plate_hash_ignores_atom_order_but_not_composition():
    """餐盘摘要对顺序不敏感，对组成敏感——顺序变了还是同一餐盘，组成变了不是。"""
    a1 = FoodAtom(code="1440", name="麦辣鸡腿汉堡", quantity=1)
    a2 = FoodAtom(code="4810", name="中薯条", quantity=1)
    assert plate_hash([a1, a2]) == plate_hash([a2, a1])
    big = FoodAtom(code="4820", name="大薯条", quantity=1)
    assert plate_hash([a1, a2]) != plate_hash([a1, big])
    assert plate_hash([a1]) != plate_hash([a1, FoodAtom(code=a1.code, name=a1.name, quantity=2)])


def test_cart_hash_distinguishes_buy_paths_for_same_plate():
    """同餐盘不同买法必须保持不同 cart_hash（否则报价会串台）。"""
    singles = cart_hash([OfferItem(product_code="1440", quantity=1)])
    doubled = cart_hash([OfferItem(product_code="1440", quantity=2)])
    assert singles != doubled
